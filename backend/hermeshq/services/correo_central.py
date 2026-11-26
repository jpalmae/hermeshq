from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from hermeshq.services import graph_proxy as gp

MAX_BYTES_ADJUNTOS = 25_000_000
MAX_BYTES_ENVIO_DIRECTO = 2_800_000
MAX_BYTES_ADJUNTO_POR_POST = 2_800_000
BYTES_POR_TROZO = 327_680 * 10
CABECERA_APROBACION = "x-hermeshq-aprobacion"
HOSTS_SUBIDA = frozenset({"outlook.office.com", "outlook.office365.com", "graph.microsoft.com"})


class EnvioRechazado(ValueError):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def _destinatarios(direcciones: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": d}} for d in direcciones]


def construir_mensaje(canon: dict, adjuntos_subidos: list[dict], aprobacion_id: str) -> dict:
    por_huella: dict[str, dict] = {}
    total = 0
    for item in adjuntos_subidos or []:
        try:
            datos = base64.b64decode(str(item.get("contenido_b64") or "").encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise EnvioRechazado("adjunto_no_valido")
        total += len(datos)
        por_huella[hashlib.sha256(datos).hexdigest()] = {**item, "_bytes": datos}
    if total > MAX_BYTES_ADJUNTOS:
        raise EnvioRechazado("adjuntos_demasiado_grandes")
    adjuntos = []
    for esperado in canon.get("adjuntos") or []:
        subido = por_huella.get(esperado["sha256"])
        if subido is None:
            raise EnvioRechazado("adjunto_distinto_de_lo_aprobado")
        if len(subido["_bytes"]) != int(esperado.get("bytes") or 0):
            raise EnvioRechazado("adjunto_distinto_de_lo_aprobado")
        adjunto = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": esperado.get("nombre") or esperado.get("content_id") or "adjunto",
            "contentType": str(subido.get("content_type") or "application/octet-stream"),
            "contentBytes": base64.b64encode(subido["_bytes"]).decode("ascii"),
        }
        if esperado.get("content_id"):
            adjunto["contentId"] = esperado["content_id"]
            adjunto["isInline"] = True
        adjuntos.append(adjunto)
    sobrantes = set(por_huella) - {a["sha256"] for a in canon.get("adjuntos") or []}
    if sobrantes:
        raise EnvioRechazado("adjunto_no_aprobado")
    mensaje = {
        "subject": canon["asunto"],
        "body": {"contentType": "HTML", "content": canon["cuerpo_html"]},
        "toRecipients": _destinatarios(canon["para"]),
        "internetMessageHeaders": [{"name": CABECERA_APROBACION, "value": aprobacion_id}],
    }
    if canon.get("cc"):
        mensaje["ccRecipients"] = _destinatarios(canon["cc"])
    if canon.get("cco"):
        mensaje["bccRecipients"] = _destinatarios(canon["cco"])
    if adjuntos:
        mensaje["attachments"] = adjuntos
    return mensaje


def bytes_de_adjuntos(mensaje: dict) -> int:
    return sum(len(base64.b64decode(a.get("contentBytes") or "")) for a in mensaje.get("attachments") or [])


def requiere_borrador(mensaje: dict) -> bool:
    return bytes_de_adjuntos(mensaje) > MAX_BYTES_ENVIO_DIRECTO


def _host_de_subida_permitido(url: str) -> bool:
    destino = urllib.parse.urlsplit(url)
    if destino.scheme == "https" and (destino.hostname or "").lower() in HOSTS_SUBIDA:
        return True
    simulado = urllib.parse.urlsplit(gp.GRAPH_BASE)
    return gp.GRAPH_BASE != gp.GRAPH_BASE_REAL and (destino.scheme, destino.netloc) == (
        simulado.scheme,
        simulado.netloc,
    )


def _subir_trozo(url: str, trozo: bytes, inicio: int, total: int) -> int:
    req = urllib.request.Request(
        url,
        data=trozo,
        method="PUT",
        headers={
            "Content-Length": str(len(trozo)),
            "Content-Range": f"bytes {inicio}-{inicio + len(trozo) - 1}/{total}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read(65536)
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def adjuntar_a_borrador(token: str, borrador_id: str, adjunto: dict) -> None:
    datos = base64.b64decode(adjunto.get("contentBytes") or "")
    base = f"/me/messages/{borrador_id}/attachments"
    if len(datos) <= MAX_BYTES_ADJUNTO_POR_POST:
        estado, _ = _post_json(token, base, adjunto)
        if not (200 <= estado < 300):
            raise EnvioRechazado(f"adjunto_no_agregado:{estado}")
        return
    item = {"attachmentType": "file", "name": adjunto.get("name") or "adjunto", "size": len(datos)}
    if adjunto.get("contentType"):
        item["contentType"] = adjunto["contentType"]
    if adjunto.get("contentId"):
        item["contentId"] = adjunto["contentId"]
        item["isInline"] = True
    estado, sesion = _post_json(token, f"{base}/createUploadSession", {"AttachmentItem": item})
    url = sesion.get("uploadUrl") if 200 <= estado < 300 else None
    if not url:
        raise EnvioRechazado(f"sesion_de_carga_no_creada:{estado}")
    if not _host_de_subida_permitido(url):
        raise EnvioRechazado("sesion_de_carga_host_no_permitido")
    for inicio in range(0, len(datos), BYTES_POR_TROZO):
        trozo = datos[inicio : inicio + BYTES_POR_TROZO]
        estado = _subir_trozo(url, trozo, inicio, len(datos))
        if not (200 <= estado < 300):
            raise EnvioRechazado(f"trozo_no_subido:{estado}")


def borrar_borrador(token: str, borrador_id: str) -> None:
    try:
        gp._llamar("DELETE", f"{gp.GRAPH_BASE}/me/messages/{borrador_id}", token, None, None, 100_000)
    except Exception:  # noqa: BLE001
        pass


def preparar_borrador_nuevo(token: str, mensaje: dict) -> str:
    sin_adjuntos = {k: v for k, v in mensaje.items() if k != "attachments"}
    estado, borrador = _post_json(token, "/me/messages", sin_adjuntos)
    if not (200 <= estado < 300) or not borrador.get("id"):
        raise EnvioRechazado(f"borrador_no_creado:{estado}")
    try:
        for adjunto in mensaje.get("attachments") or []:
            adjuntar_a_borrador(token, borrador["id"], adjunto)
    except Exception:
        borrar_borrador(token, borrador["id"])
        raise
    return borrador["id"]


def enviar(token: str, mensaje: dict) -> tuple[int, bytes]:
    cuerpo = json.dumps({"message": mensaje, "saveToSentItems": True}).encode("utf-8")
    return gp._llamar("POST", f"{gp.GRAPH_BASE}/me/sendMail", token, cuerpo, "application/json", 100_000)


def buscar_en_enviados(token: str, aprobacion_id: str) -> str | None:
    consulta = urllib.parse.urlencode(
        {"$top": "50", "$select": "id,internetMessageHeaders", "$orderby": "sentDateTime desc"}, safe="$,"
    )
    try:
        estado, datos = gp._llamar(
            "GET", f"{gp.GRAPH_BASE}/me/mailFolders/sentitems/messages?{consulta}", token, None, None, 5_000_000
        )
    except (OSError, http.client.HTTPException) as exc:
        raise EnvioRechazado("enviados_no_disponibles") from exc
    if estado != 200:
        raise EnvioRechazado("enviados_no_disponibles")
    for mensaje in json.loads(datos or b"{}").get("value", []):
        for cabecera in mensaje.get("internetMessageHeaders") or []:
            if (
                str(cabecera.get("name") or "").lower() == CABECERA_APROBACION
                and cabecera.get("value") == aprobacion_id
            ):
                return str(mensaje.get("id") or aprobacion_id)
    return None


def _post_json(token: str, ruta: str, cuerpo: dict | None, metodo: str = "POST") -> tuple[int, dict]:
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else b"{}"
    estado, crudo = gp._llamar(metodo, f"{gp.GRAPH_BASE}{ruta}", token, datos, "application/json", 5_000_000)
    try:
        return estado, json.loads(crudo or b"{}")
    except ValueError:
        return estado, {}


def adjuntos_verificados(canon: dict, adjuntos_subidos: list[dict]) -> list[dict]:
    return construir_mensaje(
        {
            **canon,
            "asunto": canon.get("asunto") or "x",
            "cuerpo_html": canon.get("cuerpo_html") or "x",
            "para": canon.get("para") or [],
            "cc": [],
            "cco": [],
        },
        adjuntos_subidos,
        "-",
    ).get("attachments", [])


def preparar_respuesta_o_reenvio(token: str, tipo: str, canon: dict, adjuntos: list[dict], aprobacion_id: str) -> str:
    origen = canon["mensaje_origen_id"]
    cabeceras = [{"name": CABECERA_APROBACION, "value": aprobacion_id}]
    destinatarios = {"toRecipients": _destinatarios(canon["para"]), "ccRecipients": _destinatarios(canon["cc"])}
    if tipo == "correo.reenviar":
        estado, borrador = _post_json(
            token,
            f"/me/messages/{origen}/createForward",
            {
                "comment": canon.get("comentario") or "",
                "message": {**destinatarios, "internetMessageHeaders": cabeceras},
            },
        )
    else:
        accion = "createReplyAll" if canon.get("responder_a_todos") else "createReply"
        estado, borrador = _post_json(
            token,
            f"/me/messages/{origen}/{accion}",
            {"message": {**destinatarios, "internetMessageHeaders": cabeceras}},
        )
    if not (200 <= estado < 300) or not borrador.get("id"):
        raise EnvioRechazado(f"borrador_no_creado:{estado}")
    borrador_id = borrador["id"]
    cambios = dict(destinatarios)
    if tipo == "correo.responder":
        citado = ((borrador.get("body") or {}).get("content")) or ""
        cambios["body"] = {"contentType": "HTML", "content": canon["cuerpo_html"] + citado}
    estado, _ = _post_json(token, f"/me/messages/{borrador_id}", cambios, metodo="PATCH")
    if not (200 <= estado < 300):
        raise EnvioRechazado(f"borrador_no_actualizado:{estado}")
    try:
        for adjunto in adjuntos:
            adjuntar_a_borrador(token, borrador_id, adjunto)
    except Exception:
        borrar_borrador(token, borrador_id)
        raise
    return borrador_id


def enviar_borrador(token: str, borrador_id: str) -> tuple[int, bytes]:
    return gp._llamar(
        "POST", f"{gp.GRAPH_BASE}/me/messages/{borrador_id}/send", token, b"{}", "application/json", 100_000
    )
