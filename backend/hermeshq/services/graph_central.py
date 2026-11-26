from __future__ import annotations

import json
import re
import urllib.parse

from hermeshq.services import graph_proxy as gp

PREFIJO_DIRECTO = "directo:"
CARPETA_TEAMS = "HermesHQ Uploads"
MAX_BYTES_ARCHIVO_TEAMS = 4 * 1024 * 1024
_RUTA_MENSAJE = re.compile(r"^/me/chats/([^/]+)/messages$")


class AccionRechazada(ValueError):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def chat_de_ruta(ruta: str) -> str:
    m = _RUTA_MENSAJE.match(ruta or "")
    if not m:
        raise AccionRechazada("ruta_de_chat_invalida")
    return m.group(1)


def destinatario_directo(ruta: str) -> str | None:
    chat = chat_de_ruta(ruta)
    return chat[len(PREFIJO_DIRECTO) :] if chat.startswith(PREFIJO_DIRECTO) else None


def _get(token: str, ruta: str) -> tuple[int, dict]:
    estado, datos = gp._llamar("GET", f"{gp.GRAPH_BASE}{ruta}", token, None, None, 2_000_000)
    try:
        return estado, json.loads(datos or b"{}")
    except ValueError:
        return estado, {}


def _post(token: str, ruta: str, cuerpo: dict) -> tuple[int, dict]:
    estado, datos = gp._llamar(
        "POST", f"{gp.GRAPH_BASE}{ruta}", token, json.dumps(cuerpo).encode("utf-8"), "application/json", 2_000_000
    )
    try:
        return estado, json.loads(datos or b"{}")
    except ValueError:
        return estado, {}


def _identidad(usuario: dict) -> str:
    return str(usuario.get("mail") or usuario.get("userPrincipalName") or usuario.get("id") or "").strip().lower()


def miembros_del_chat(token: str, ruta: str) -> list[str]:
    directo = destinatario_directo(ruta)
    if directo is not None:
        estado_yo, yo = _get(token, "/me?$select=id,mail,userPrincipalName")
        estado_otro, otro = _get(token, f"/users/{urllib.parse.quote(directo)}?$select=id,mail,userPrincipalName")
        if estado_yo != 200 or estado_otro != 200 or not _identidad(otro):
            raise AccionRechazada("destinatario_no_resuelto")
        return sorted({_identidad(yo), _identidad(otro)} - {""})
    estado, datos = _get(token, f"/me/chats/{chat_de_ruta(ruta)}/members")
    if estado != 200:
        raise AccionRechazada("chat_no_resuelto")
    miembros = []
    for m in datos.get("value", []):
        identidad = str(m.get("email") or m.get("userId") or "").strip().lower()
        if identidad:
            miembros.append(identidad)
    if not miembros:
        raise AccionRechazada("chat_sin_miembros")
    return sorted(set(miembros))


def archivo_de_item(token: str, ruta: str) -> dict:
    m = re.match(r"^/me/drive/items/([^/]+)/createLink$", ruta or "")
    if not m:
        raise AccionRechazada("ruta_de_archivo_invalida")
    estado, item = _get(token, f"/me/drive/items/{m.group(1)}?$select=id,name,parentReference,file")
    if estado != 200 or not item.get("name"):
        raise AccionRechazada("archivo_no_resuelto")
    carpeta = str((item.get("parentReference") or {}).get("path") or "").split("root:", 1)[-1].strip("/")
    return {"nombre": str(item["name"]), "ruta": carpeta}


def buscar_chat_directo(token: str, usuario_id: str) -> str | None:
    consulta = urllib.parse.quote("chatType eq 'oneOnOne'")
    estado, datos = _get(token, f"/me/chats?$filter={consulta}&$expand=members&$top=50")
    if estado != 200:
        raise AccionRechazada(f"chats_no_disponibles:{estado}")
    for chat in datos.get("value", []):
        if any(str(m.get("userId") or "") == usuario_id for m in chat.get("members") or []):
            return str(chat.get("id"))
    return None


def crear_o_obtener_chat_directo(token: str, usuario_id: str) -> str:
    estado_yo, yo = _get(token, "/me?$select=id")
    if estado_yo != 200 or not yo.get("id"):
        raise AccionRechazada("identidad_no_resuelta")
    base = f"{gp.GRAPH_BASE_REAL}/users"
    miembros = [
        {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"{base}('{uid}')",
        }
        for uid in (yo["id"], usuario_id)
    ]
    estado, chat = _post(token, "/chats", {"chatType": "oneOnOne", "members": miembros})
    if not (200 <= estado < 300) or not chat.get("id"):
        raise AccionRechazada(f"chat_no_creado:{estado}")
    return str(chat["id"])


def subir_archivo(token: str, aprobacion_id: str, archivo: dict, datos: bytes) -> dict:
    nombre = re.sub(r"[^A-Za-z0-9._ -]", "_", str(archivo.get("nombre") or "archivo")).strip(" .")[:120] or "archivo"
    destino = urllib.parse.quote(f"{CARPETA_TEAMS}/{aprobacion_id[:8]}_{nombre}")
    estado, crudo = gp._llamar(
        "PUT",
        f"{gp.GRAPH_BASE}/me/drive/root:/{destino}:/content?@microsoft.graph.conflictBehavior=replace",
        token,
        datos,
        "application/octet-stream",
        2_000_000,
    )
    try:
        item = json.loads(crudo or b"{}")
    except ValueError:
        item = {}
    if not (200 <= estado < 300) or not item.get("id"):
        raise AccionRechazada(f"archivo_no_subido:{estado}")
    return item


def _mensaje_con_archivo(cuerpo: dict, item: dict) -> dict:
    texto = str(((cuerpo.get("body") or {}).get("content")) or "")
    adjunto_id = item["id"]
    return {
        "body": {"contentType": "html", "content": f'{texto}<attachment id="{adjunto_id}"></attachment>'},
        "attachments": [
            {
                "id": adjunto_id,
                "contentType": "reference",
                "contentUrl": item.get("webUrl", ""),
                "name": item.get("name", ""),
            }
        ],
    }


def ejecutar(
    token: str, canon: dict, aprobacion_id: str | None = None, archivo: bytes | None = None
) -> tuple[int, dict]:
    metodo, ruta, cuerpo = canon["metodo"], canon["ruta"], canon["cuerpo"]
    if metodo == "POST" and _RUTA_MENSAJE.match(ruta):
        directo = destinatario_directo(ruta)
        if directo is not None:
            chat = buscar_chat_directo(token, directo) or crear_o_obtener_chat_directo(token, directo)
            ruta = f"/me/chats/{chat}/messages"
        if canon.get("archivo"):
            if archivo is None:
                raise AccionRechazada("falta_el_archivo_aprobado")
            cuerpo = _mensaje_con_archivo(cuerpo, subir_archivo(token, aprobacion_id or "x", canon["archivo"], archivo))
    datos_cuerpo = None if metodo == "DELETE" else json.dumps(cuerpo).encode("utf-8")
    estado, datos = gp._llamar(
        metodo,
        f"{gp.GRAPH_BASE}{ruta}",
        token,
        datos_cuerpo,
        "application/json" if datos_cuerpo else None,
        2_000_000,
    )
    try:
        return estado, json.loads(datos or b"{}")
    except ValueError:
        return estado, {}
