from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update

from hermeshq.models.aprobacion import Aprobacion, EventoAprobacion

logger = logging.getLogger(__name__)

VERSION_CANONICA = 1
TTL_POR_DEFECTO = timedelta(hours=4)
TTL_EJECUCION_COLGADA = timedelta(minutes=15)
ESTADOS = ("pendiente", "aprobada", "rechazada", "expirada", "ejecutando", "ejecutada", "fallida")
ENVIO_ESTADOS = ("sin_envio", "enviando", "aceptada_proveedor", "confirmada", "entregada", "fallida", "desconocida")
ENVIO_TRANSICIONES = {
    "sin_envio": {"enviando"},
    "enviando": {"aceptada_proveedor", "confirmada", "fallida", "desconocida"},
    "aceptada_proveedor": {"confirmada", "entregada", "desconocida"},
    "confirmada": {"entregada"},
    "desconocida": {"confirmada", "enviando", "fallida", "sin_envio"},
    "entregada": set(),
    "fallida": set(),
}
FRASE_ENVIO = {
    "sin_envio": "Aprobado, todavía no enviado.",
    "enviando": "Envío en curso.",
    "aceptada_proveedor": "El servidor aceptó el correo; todavía no lo verifiqué en Enviados.",
    "confirmada": "Enviado y verificado en el buzón de enviados.",
    "entregada": "Entregado (hay evidencia de entrega).",
    "fallida": "No se envió.",
    "desconocida": "No sé si se envió; lo estoy verificando antes de hacer nada más.",
}
ESPERA_CONCILIACION = timedelta(minutes=30)
ESTADOS_FINALES = ("rechazada", "expirada", "ejecutada", "fallida")
TIPO_CORREO = "correo.enviar"
_CORREO_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

MENSAJES = {
    "no_encontrada": "No existe esa aprobación.",
    "otro_usuario": "La aprobación pertenece a otro usuario o a otro agente.",
    "otra_conversacion": "La aprobación se pidió en otra conversación; no puede usarse aquí.",
    "huella_distinta": "El contenido cambió respecto de lo aprobado; hace falta una aprobación nueva.",
    "pendiente": "El usuario todavía no aprueba esta acción.",
    "rechazada": "El usuario rechazó esta acción.",
    "expirada": "La aprobación expiró; hace falta una nueva.",
    "ya_ejecutada": "Esta acción ya se ejecutó (o está en curso); no se repite.",
    "fallida": "La ejecución anterior falló; hace falta una aprobación nueva.",
    "no_atendida": "Una ejecución programada o en segundo plano no puede ejecutar acciones con efecto.",
    "estado_invalido": "La aprobación no está en un estado que permita esa operación.",
    "sin_conversacion": "La acción no tiene una conversación de usuario a la cual vincularse.",
    "operacion_bloqueada": "La política del administrador no permite esta operación a este agente.",
    "usuario_no_humano": "El solicitante debe ser un usuario humano identificado (no la clave del bridge).",
    "tarea_invalida": "La tarea no está en curso para este agente.",
    "contenido_invalido": "El contenido de la acción está incompleto o es inválido.",
    "tipo_desconocido": "Tipo de acción no soportado.",
}


class ErrorAprobacion(Exception):
    def __init__(self, codigo: str, detalle: str | None = None):
        self.codigo = codigo
        self.mensaje = detalle or MENSAJES.get(codigo, codigo)
        super().__init__(f"{codigo}: {self.mensaje}")


@dataclass(frozen=True)
class Contexto:
    agent_id: str
    user_id: str
    conversacion_id: str
    task_id: str | None = None
    canal: str = ""
    no_atendida: bool = False
    attempt_id: str | None = None
    lease_generation: int | None = None


def _ahora(ahora: datetime | None = None) -> datetime:
    return ahora or datetime.now(UTC)


def _aware(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=UTC)


def _texto(valor) -> str:
    texto = unicodedata.normalize("NFC", str(valor or ""))
    return texto.replace("\r\n", "\n").replace("\r", "\n")


def _correos(valor, campo: str) -> list[str]:
    if valor is None:
        return []
    if isinstance(valor, str):
        valor = [valor]
    if not isinstance(valor, list):
        raise ErrorAprobacion("contenido_invalido", f"{campo} debe ser una lista de correos.")
    vistos: set[str] = set()
    for item in valor:
        correo = _texto(item).strip().lower()
        if not _CORREO_RE.match(correo):
            raise ErrorAprobacion("contenido_invalido", f"Dirección inválida en {campo}: {item!r}.")
        vistos.add(correo)
    return sorted(vistos)


def _adjuntos(valor) -> list[dict]:
    salida = []
    for item in valor or []:
        if not isinstance(item, dict):
            raise ErrorAprobacion("contenido_invalido", "Cada adjunto debe ser un objeto.")
        sha = str(item.get("sha256") or "").lower()
        if not _HEX64.match(sha):
            raise ErrorAprobacion("contenido_invalido", "Cada adjunto debe traer su sha256.")
        salida.append(
            {
                "nombre": _texto(item.get("nombre")).strip(),
                "sha256": sha,
                "bytes": int(item.get("bytes") or 0),
                "content_id": _texto(item.get("content_id")).strip(),
            }
        )
    return sorted(salida, key=lambda a: (a["sha256"], a["nombre"], a["content_id"]))


def canonizar_correo(contenido: dict) -> dict:
    if not isinstance(contenido, dict):
        raise ErrorAprobacion("contenido_invalido")
    canon = {
        "cuenta_emisora": _texto(contenido.get("cuenta_emisora")).strip(),
        "para": _correos(contenido.get("para"), "para"),
        "cc": _correos(contenido.get("cc"), "cc"),
        "cco": _correos(contenido.get("cco"), "cco"),
        "asunto": _texto(contenido.get("asunto")).strip(),
        "cuerpo_html": _texto(contenido.get("cuerpo_html")),
        "firma": _texto(contenido.get("firma")).strip(),
        "firma_sha256": str(contenido.get("firma_sha256") or "").lower(),
        "adjuntos": _adjuntos(contenido.get("adjuntos")),
    }
    if not canon["cuenta_emisora"] or not canon["para"] or not canon["asunto"] or not canon["cuerpo_html"].strip():
        raise ErrorAprobacion("contenido_invalido", "Faltan cuenta emisora, destinatarios, asunto o cuerpo.")
    return canon


TIPO_RESPONDER = "correo.responder"
TIPO_REENVIAR = "correo.reenviar"
TIPOS_CORREO = (TIPO_CORREO, TIPO_RESPONDER, TIPO_REENVIAR)
_ID_ORIGEN = re.compile(r"^[A-Za-z0-9_\-=+/.!]{8,400}$")


def _origen(contenido: dict) -> str:
    origen = _texto(contenido.get("mensaje_origen_id")).strip()
    if not _ID_ORIGEN.match(origen) or ".." in origen:
        raise ErrorAprobacion("contenido_invalido", "Falta el id del correo original.")
    return origen


def canonizar_responder(contenido: dict) -> dict:
    if not isinstance(contenido, dict):
        raise ErrorAprobacion("contenido_invalido")
    canon = {
        "cuenta_emisora": _texto(contenido.get("cuenta_emisora")).strip(),
        "mensaje_origen_id": _origen(contenido),
        "responder_a_todos": bool(contenido.get("responder_a_todos")),
        "para": _correos(contenido.get("para"), "para"),
        "cc": _correos(contenido.get("cc"), "cc"),
        "asunto": _texto(contenido.get("asunto")).strip(),
        "cuerpo_html": _texto(contenido.get("cuerpo_html")),
        "firma": _texto(contenido.get("firma")).strip(),
        "firma_sha256": str(contenido.get("firma_sha256") or "").lower(),
        "adjuntos": _adjuntos(contenido.get("adjuntos")),
    }
    if not canon["cuenta_emisora"] or not canon["para"] or not canon["cuerpo_html"].strip():
        raise ErrorAprobacion("contenido_invalido", "Faltan cuenta emisora, destinatarios o texto de la respuesta.")
    return canon


def canonizar_reenviar(contenido: dict) -> dict:
    if not isinstance(contenido, dict):
        raise ErrorAprobacion("contenido_invalido")
    canon = {
        "cuenta_emisora": _texto(contenido.get("cuenta_emisora")).strip(),
        "mensaje_origen_id": _origen(contenido),
        "para": _correos(contenido.get("para"), "para"),
        "cc": _correos(contenido.get("cc"), "cc"),
        "asunto": _texto(contenido.get("asunto")).strip(),
        "comentario": _texto(contenido.get("comentario")),
        "adjuntos_originales": sorted(_texto(n).strip() for n in contenido.get("adjuntos_originales") or []),
    }
    if not canon["cuenta_emisora"] or not canon["para"]:
        raise ErrorAprobacion("contenido_invalido", "Faltan cuenta emisora o destinatarios.")
    return canon


TIPO_CALENDARIO = "graph.calendario"
TIPO_TEAMS = "graph.teams"
TIPO_ONEDRIVE = "graph.onedrive"
TIPOS_GRAPH = (TIPO_CALENDARIO, TIPO_TEAMS, TIPO_ONEDRIVE)
ALCANCES_ENLACE = {"organization": "solo personas de la organización", "anonymous": "cualquiera que tenga el enlace"}
_ID_GRAPH = r"[A-Za-z0-9_\-=+/.!:@]{4,400}"
RUTAS_PERMITIDAS = {
    TIPO_CALENDARIO: (
        ("POST", re.compile(r"^/me/events$")),
        ("PATCH", re.compile(rf"^/me/events/{_ID_GRAPH}$")),
        ("DELETE", re.compile(rf"^/me/events/{_ID_GRAPH}$")),
        ("POST", re.compile(rf"^/me/events/{_ID_GRAPH}/(accept|decline|tentativelyAccept)$")),
    ),
    TIPO_TEAMS: (("POST", re.compile(rf"^/me/chats/{_ID_GRAPH}/messages$")),),
    TIPO_ONEDRIVE: (("POST", re.compile(r"^/me/drive/items/[A-Za-z0-9!_\-]{4,200}/createLink$")),),
}


def canonizar_graph(tipo: str, contenido: dict) -> dict:
    if not isinstance(contenido, dict):
        raise ErrorAprobacion("contenido_invalido")
    metodo = _texto(contenido.get("metodo")).strip().upper()
    ruta = _texto(contenido.get("ruta")).strip()
    if (
        ".." in ruta
        or "?" in ruta
        or not any(metodo == m and patron.match(ruta) for m, patron in RUTAS_PERMITIDAS[tipo])
    ):
        raise ErrorAprobacion("contenido_invalido", f"Operación no permitida para {tipo}: {metodo} {ruta}")
    cuerpo = contenido.get("cuerpo") or {}
    if not isinstance(cuerpo, dict):
        raise ErrorAprobacion("contenido_invalido", "El cuerpo debe ser un objeto.")
    canon = {"metodo": metodo, "ruta": ruta, "cuerpo": json.loads(json.dumps(cuerpo, sort_keys=True))}
    if tipo == TIPO_ONEDRIVE:
        if (
            set(cuerpo) != {"type", "scope"}
            or cuerpo.get("type") != "view"
            or cuerpo.get("scope") not in ALCANCES_ENLACE
        ):
            raise ErrorAprobacion(
                "contenido_invalido", "Solo se comparten enlaces de lectura (organization o anonymous)."
            )
        archivo = contenido.get("archivo")
        if not isinstance(archivo, dict) or not _texto(archivo.get("nombre")).strip():
            raise ErrorAprobacion("contenido_invalido", "Falta el archivo resuelto por el servidor.")
        canon["archivo"] = {
            "nombre": _texto(archivo.get("nombre")).strip(),
            "ruta": _texto(archivo.get("ruta")).strip(),
        }
    if tipo == TIPO_TEAMS:
        miembros = contenido.get("miembros")
        if not isinstance(miembros, list) or not miembros:
            raise ErrorAprobacion("contenido_invalido", "Faltan los miembros del chat resueltos por el servidor.")
        canon["miembros"] = sorted({_texto(m).strip().lower() for m in miembros if _texto(m).strip()})
        archivo = contenido.get("archivo")
        if archivo is not None:
            if not isinstance(archivo, dict):
                raise ErrorAprobacion("contenido_invalido", "Archivo inválido.")
            nombre = _texto(archivo.get("nombre")).strip()
            sha = _texto(archivo.get("sha256")).strip().lower()
            tam = archivo.get("bytes")
            if (
                not nombre
                or len(nombre) > 255
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or not isinstance(tam, int)
                or isinstance(tam, bool)
                or not 0 < tam <= 4 * 1024 * 1024
            ):
                raise ErrorAprobacion("contenido_invalido", "Archivo inválido o de más de 4 MB.")
            canon["archivo"] = {"nombre": nombre, "sha256": sha, "bytes": tam}
    return canon


CANONIZADORES = {
    TIPO_CORREO: canonizar_correo,
    TIPO_RESPONDER: canonizar_responder,
    TIPO_REENVIAR: canonizar_reenviar,
    TIPO_CALENDARIO: lambda c: canonizar_graph(TIPO_CALENDARIO, c),
    TIPO_TEAMS: lambda c: canonizar_graph(TIPO_TEAMS, c),
    TIPO_ONEDRIVE: lambda c: canonizar_graph(TIPO_ONEDRIVE, c),
}


def _vista_graph(tipo: str, canon: dict, huella: str) -> str:
    cuerpo = canon["cuerpo"]
    lineas = []
    if tipo == TIPO_ONEDRIVE:
        archivo = canon["archivo"]
        alcance = cuerpo["scope"]
        lineas += [
            "**Acción:** Compartir archivo de OneDrive (enlace de solo lectura)",
            f"**Archivo:** {'/'.join(p for p in (archivo['ruta'], archivo['nombre']) if p)}",
            f"**Quién podrá abrirlo:** {ALCANCES_ENLACE[alcance]}",
        ]
    elif tipo == TIPO_TEAMS:
        texto = html_a_texto(((cuerpo.get("body") or {}).get("content")) or "")
        chat = canon["ruta"].split("/")[3]
        lineas += [
            "**Acción:** Enviar mensaje de Teams"
            + (" (abre el chat directo si no existe)" if chat.startswith("directo:") else ""),
            f"**Chat:** {'directo' if chat.startswith('directo:') else chat}",
            f"**Miembros del chat:** {', '.join(canon['miembros'])}",
        ]
        if canon.get("archivo"):
            archivo = canon["archivo"]
            lineas.append(
                f"**Archivo:** {archivo['nombre']} ({archivo['bytes'] / 1024:.0f} KB, sha256 {archivo['sha256'][:12]}…)"
            )
        lineas += ["", texto]
    else:
        ruta, metodo = canon["ruta"], canon["metodo"]
        accion = {"POST": "Crear evento", "PATCH": "Modificar evento", "DELETE": "Eliminar evento"}.get(metodo, "")
        if ruta.rsplit("/", 1)[-1] in ("accept", "decline", "tentativelyAccept"):
            accion = {"accept": "Aceptar", "decline": "Rechazar", "tentativelyAccept": "Aceptar provisionalmente"}[
                ruta.rsplit("/", 1)[-1]
            ] + " invitación"
        lineas.append(f"**Acción:** {accion}")
        if metodo != "POST" or ruta != "/me/events":
            lineas.append(f"**Evento:** {ruta.split('/')[3]}")
        if cuerpo.get("subject"):
            lineas.append(f"**Asunto:** {cuerpo['subject']}")
        for campo, nombre in (("start", "Inicio"), ("end", "Fin")):
            if isinstance(cuerpo.get(campo), dict):
                lineas.append(f"**{nombre}:** {cuerpo[campo].get('dateTime')} ({cuerpo[campo].get('timeZone')})")
        if isinstance(cuerpo.get("attendees"), list):
            correos = [((a or {}).get("emailAddress") or {}).get("address") for a in cuerpo["attendees"]]
            lineas.append(f"**Asistentes (reciben invitación):** {', '.join(c for c in correos if c) or 'ninguno'}")
        if isinstance(cuerpo.get("location"), dict) and cuerpo["location"].get("displayName"):
            lineas.append(f"**Lugar:** {cuerpo['location']['displayName']}")
        if "sendResponse" in cuerpo:
            lineas.append(f"**Avisa al organizador:** {'sí' if cuerpo['sendResponse'] else 'no'}")
        if cuerpo.get("comment"):
            lineas.append(f"**Comentario:** {cuerpo['comment']}")
        if isinstance(cuerpo.get("body"), dict):
            lineas += ["", html_a_texto(cuerpo["body"].get("content") or "")]
    return (
        "\n".join(lineas)
        + f"\n\n---\n\n_Versión aprobable {huella[:12]}: cualquier cambio exige una aprobación nueva._"
    )


def canonizar(tipo: str, contenido: dict) -> dict:
    canonizador = CANONIZADORES.get(tipo)
    if canonizador is None:
        raise ErrorAprobacion("tipo_desconocido")
    return canonizador(contenido)


def huella_canonica(tipo: str, contenido: dict) -> str:
    canon = canonizar(tipo, contenido)
    serial = json.dumps(
        {"tipo": tipo, "v": VERSION_CANONICA, "accion": canon},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def html_a_texto(cuerpo_html: str) -> str:
    crudo = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", cuerpo_html or "")
    crudo = re.sub(r"(?i)<br\s*/?>", "\n", crudo)
    crudo = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n\n", crudo)
    crudo = re.sub(r"(?i)<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"[imagen: \1]", crudo)
    texto = html.unescape(re.sub(r"<[^>]+>", "", crudo))
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def vista_previa(tipo: str, canon: dict, huella: str) -> str:
    if tipo in TIPOS_GRAPH:
        return _vista_graph(tipo, canon, huella)
    if tipo not in TIPOS_CORREO:
        raise ErrorAprobacion("tipo_desconocido")
    accion = {
        TIPO_CORREO: "Enviar correo",
        TIPO_RESPONDER: "Responder" + (" a todos" if canon.get("responder_a_todos") else ""),
        TIPO_REENVIAR: "Reenviar",
    }[tipo]
    lineas = [
        f"**Acción:** {accion}",
        f"**Cuenta emisora:** {canon['cuenta_emisora']}",
        f"**Para:** {', '.join(canon['para'])}",
    ]
    if tipo == TIPO_REENVIAR:
        if canon["cc"]:
            lineas.append(f"**CC:** {', '.join(canon['cc'])}")
        lineas.append(f"**Asunto:** {canon['asunto']}")
        if canon["adjuntos_originales"]:
            lineas.append(f"**Adjuntos del original que viajan:** {', '.join(canon['adjuntos_originales'])}")
        return (
            "\n".join(lineas)
            + "\n\n---\n\n"
            + (canon["comentario"] or "(sin comentario)")
            + "\n\n_Se reenvía el correo original completo debajo del comentario._\n\n---\n\n"
            + f"_Versión aprobable {huella[:12]}: cualquier cambio exige una aprobación nueva._"
        )
    if canon["cc"]:
        lineas.append(f"**CC:** {', '.join(canon['cc'])}")
    if canon.get("cco"):
        lineas.append(f"**CCO:** {', '.join(canon['cco'])}")
    lineas.append(f"**Asunto:** {canon['asunto']}")
    if canon["adjuntos"]:
        partes = [
            f"{a['nombre'] or a['content_id']} ({a['bytes']} B, sha256 {a['sha256'][:12]})" for a in canon["adjuntos"]
        ]
        lineas.append(f"**Adjuntos:** {'; '.join(partes)}")
    lineas.append(f"**Firma:** {canon['firma'] or 'sin firma'}")
    return (
        "\n".join(lineas)
        + "\n\n---\n\n"
        + html_a_texto(canon["cuerpo_html"])
        + ("\n\n_Debajo va el hilo original citado._" if tipo == TIPO_RESPONDER else "")
        + "\n\n---\n\n"
        + f"_Versión aprobable {huella[:12]}: cualquier cambio exige una aprobación nueva._"
    )


TITULOS_TARJETA = {
    TIPO_CORREO: "Aprobación requerida: enviar correo",
    TIPO_RESPONDER: "Aprobación requerida: responder correo",
    TIPO_REENVIAR: "Aprobación requerida: reenviar correo",
    TIPO_CALENDARIO: "Aprobación requerida: calendario",
    TIPO_TEAMS: "Aprobación requerida: mensaje de Teams",
    TIPO_ONEDRIVE: "Aprobación requerida: compartir archivo",
}


def tarjeta_adaptativa(aprob: Aprobacion) -> dict:

    def accion(titulo: str, decision: str) -> dict:
        return {
            "type": "Action.Submit",
            "title": titulo,
            "data": {
                "hq_accion": "aprobacion.decidir",
                "aprobacion_id": aprob.id,
                "huella": aprob.huella,
                "decision": decision,
            },
        }

    expira = _aware(aprob.expira_en)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": TITULOS_TARJETA.get(aprob.tipo_accion, "Aprobación requerida"),
                "weight": "Bolder",
                "wrap": True,
            },
            {"type": "TextBlock", "text": aprob.vista_previa, "wrap": True, "fontType": "Monospace"},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Versión", "value": aprob.huella[:12]},
                    {"title": "Expira", "value": expira.strftime("%Y-%m-%d %H:%M UTC") if expira else ""},
                ],
            },
        ],
        "actions": [accion("Aprobar", "aprobar"), accion("Rechazar", "rechazar")],
    }


def tarjeta_decidida(aprob: Aprobacion) -> dict:
    textos = {
        "aprobada": "Aprobado",
        "rechazada": "Rechazado",
        "expirada": "Expirado",
        "ejecutada": "Ejecutado",
        "ejecutando": "En ejecución",
        "fallida": "No se ejecutó",
    }
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{textos.get(aprob.estado, aprob.estado)} · versión {aprob.huella[:12]}",
                "weight": "Bolder",
                "wrap": True,
            },
            {"type": "TextBlock", "text": aprob.vista_previa, "wrap": True, "fontType": "Monospace", "isSubtle": True},
        ],
    }


def resumen(aprob: Aprobacion) -> dict:
    return {
        "id": aprob.id,
        "tipo_accion": aprob.tipo_accion,
        "estado": aprob.estado,
        "agent_id": aprob.agent_id,
        "propietario_user_id": aprob.propietario_user_id,
        "conversacion_id": aprob.conversacion_id,
        "huella": aprob.huella,
        "vista_previa": aprob.vista_previa,
        "contenido": aprob.contenido_json,
        "creado_en": _aware(aprob.creado_en).isoformat() if aprob.creado_en else None,
        "expira_en": _aware(aprob.expira_en).isoformat() if aprob.expira_en else None,
        "decidido_por_user_id": aprob.decidido_por_user_id,
        "canal_decision": aprob.canal_decision,
        "resultado": aprob.resultado_json,
        "envio_estado": aprob.envio_estado,
        "requiere_conciliacion": aprob.estado == "ejecutando" and aprob.envio_estado == "desconocida",
    }


def _evento(
    db,
    approval_id: str,
    evento: str,
    anterior,
    nuevo,
    actor_tipo: str,
    actor_id,
    canal=None,
    task_id=None,
    detalle=None,
):
    db.add(
        EventoAprobacion(
            approval_id=approval_id,
            evento=evento,
            estado_anterior=anterior,
            estado_nuevo=nuevo,
            actor_tipo=actor_tipo,
            actor_id=actor_id,
            canal=canal,
            task_id=task_id,
            detalle_json=detalle,
        )
    )


async def _cargar(db, approval_id: str) -> Aprobacion | None:
    if not approval_id:
        return None
    return await db.get(Aprobacion, approval_id, populate_existing=True)


async def _transicion(db, approval_id: str, desde: tuple[str, ...], hacia: str, *condiciones, **valores) -> bool:
    stmt = (
        update(Aprobacion)
        .where(Aprobacion.id == approval_id, Aprobacion.estado.in_(desde), *condiciones)
        .values(estado=hacia, **valores)
        .execution_options(synchronize_session=False)
    )
    resultado = await db.execute(stmt)
    return resultado.rowcount == 1


async def _fallar(
    db,
    codigo: str,
    approval_id: str | None = None,
    actor_tipo="sistema",
    actor_id=None,
    canal=None,
    task_id=None,
    detalle=None,
):
    if approval_id:
        _evento(db, approval_id, f"denegado:{codigo}", None, None, actor_tipo, actor_id, canal, task_id, detalle)
        await db.commit()
    raise ErrorAprobacion(codigo)


def _validar_contexto(ctx: Contexto) -> None:
    if not ctx.user_id:
        raise ErrorAprobacion("usuario_no_humano")
    if not ctx.conversacion_id or ctx.conversacion_id == ctx.user_id:
        raise ErrorAprobacion("sin_conversacion")


async def crear(
    db,
    tipo: str,
    contenido: dict,
    ctx: Contexto,
    parametros: dict | None = None,
    ttl: timedelta | None = None,
    ahora: datetime | None = None,
) -> Aprobacion:
    if ctx.no_atendida:
        raise ErrorAprobacion("no_atendida")
    _validar_contexto(ctx)
    momento = _ahora(ahora)
    canon = canonizar(tipo, contenido)
    huella = huella_canonica(tipo, canon)
    previas = (
        (
            await db.execute(
                select(Aprobacion).where(
                    Aprobacion.agent_id == ctx.agent_id,
                    Aprobacion.propietario_user_id == ctx.user_id,
                    Aprobacion.conversacion_id == ctx.conversacion_id,
                    Aprobacion.tipo_accion == tipo,
                    Aprobacion.estado.in_(("pendiente", "aprobada", "ejecutando", "ejecutada")),
                )
            )
        )
        .scalars()
        .all()
    )
    for previa in previas:
        if previa.huella != huella:
            continue
        if previa.estado in ("ejecutando", "ejecutada"):
            return previa
        if previa.estado in ("pendiente", "aprobada") and _aware(previa.expira_en) > momento:
            return previa
    nueva_id = str(uuid4())
    for previa in previas:
        if await _transicion(db, previa.id, ("pendiente", "aprobada"), "expirada", reemplazada_por=nueva_id):
            _evento(
                db,
                previa.id,
                "reemplazada",
                previa.estado,
                "expirada",
                "agente",
                ctx.agent_id,
                ctx.canal,
                ctx.task_id,
                {"por": nueva_id},
            )
    aprob = Aprobacion(
        id=nueva_id,
        tipo_accion=tipo,
        estado="pendiente",
        agent_id=ctx.agent_id,
        solicitante_user_id=ctx.user_id,
        propietario_user_id=ctx.user_id,
        conversacion_id=ctx.conversacion_id,
        canal_origen=ctx.canal,
        task_id_origen=ctx.task_id,
        attempt_id_origen=ctx.attempt_id,
        huella=huella,
        version_canonica=VERSION_CANONICA,
        contenido_json=canon,
        parametros_json=parametros,
        vista_previa=vista_previa(tipo, canon, huella),
        idempotency_key=hashlib.sha256(f"{nueva_id}:{huella}".encode()).hexdigest(),
        creado_en=momento,
        expira_en=momento + (ttl or TTL_POR_DEFECTO),
    )
    db.add(aprob)
    await db.flush()
    _evento(
        db, nueva_id, "creada", None, "pendiente", "agente", ctx.agent_id, ctx.canal, ctx.task_id, {"huella": huella}
    )
    await db.commit()
    return aprob


async def ver(db, approval_id: str, user_id: str) -> Aprobacion:
    aprob = await _cargar(db, approval_id)
    if aprob is None or aprob.propietario_user_id != user_id:
        raise ErrorAprobacion("no_encontrada")
    return aprob


async def decidir(
    db,
    approval_id: str,
    aprobar: bool,
    actor_user_id: str,
    canal: str,
    huella_vista: str,
    conversacion_id: str | None = None,
    actor_tipo: str = "usuario",
    ahora: datetime | None = None,
    ejecutar_al_aprobar: bool = True,
) -> Aprobacion:
    momento = _ahora(ahora)
    aprob = await _cargar(db, approval_id)
    if aprob is None:
        raise ErrorAprobacion("no_encontrada")
    if not actor_user_id or aprob.propietario_user_id != actor_user_id:
        await _fallar(db, "otro_usuario", aprob.id, actor_tipo, actor_user_id, canal)
    if conversacion_id is not None and conversacion_id != aprob.conversacion_id:
        await _fallar(db, "otra_conversacion", aprob.id, actor_tipo, actor_user_id, canal)
    if huella_vista != aprob.huella:
        await _fallar(
            db, "huella_distinta", aprob.id, actor_tipo, actor_user_id, canal, detalle={"huella_vista": huella_vista}
        )
    if aprob.estado == "pendiente" and _aware(aprob.expira_en) <= momento:
        if await _transicion(db, aprob.id, ("pendiente",), "expirada"):
            _evento(db, aprob.id, "expirada", "pendiente", "expirada", "sistema", None, canal)
        await db.commit()
        raise ErrorAprobacion("expirada")
    hacia = "aprobada" if aprobar else "rechazada"
    hecho = await _transicion(
        db,
        aprob.id,
        ("pendiente",),
        hacia,
        Aprobacion.propietario_user_id == actor_user_id,
        Aprobacion.huella == huella_vista,
        decidido_en=momento,
        decidido_por_user_id=actor_user_id,
        canal_decision=canal,
    )
    if not hecho:
        actual = await _cargar(db, aprob.id)
        await _fallar(
            db,
            actual.estado if actual.estado in ("expirada", "rechazada") else "estado_invalido",
            aprob.id,
            actor_tipo,
            actor_user_id,
            canal,
        )
    _evento(db, aprob.id, hacia, "pendiente", hacia, actor_tipo, actor_user_id, canal)
    await db.commit()
    if aprobar and ejecutar_al_aprobar and aprob.tipo_accion in TIPOS_GRAPH:
        from hermeshq.services import ejecucion_aprobada

        try:
            await ejecucion_aprobada.ejecutar(db, aprob.id, ahora=momento)
        except Exception:  # noqa: BLE001
            logger.exception("ejecución tras aprobar no completada: aprobacion=%s", aprob.id)
            await db.rollback()
    return await _cargar(db, aprob.id)


_CODIGO_POR_ESTADO = {
    "pendiente": "pendiente",
    "rechazada": "rechazada",
    "expirada": "expirada",
    "ejecutando": "ya_ejecutada",
    "ejecutada": "ya_ejecutada",
    "fallida": "fallida",
}


async def reclamar_ejecucion(
    db, approval_id: str, ctx: Contexto, contenido: dict, ahora: datetime | None = None
) -> Aprobacion:
    momento = _ahora(ahora)
    aprob = await _cargar(db, approval_id)
    if aprob is None:
        raise ErrorAprobacion("no_encontrada")
    actor = ("agente", ctx.agent_id, ctx.canal, ctx.task_id)
    if ctx.no_atendida:
        await _fallar(db, "no_atendida", aprob.id, *actor)
    if aprob.agent_id != ctx.agent_id or aprob.propietario_user_id != ctx.user_id:
        await _fallar(db, "otro_usuario", aprob.id, *actor, detalle={"user_id": ctx.user_id})
    if not ctx.conversacion_id or aprob.conversacion_id != ctx.conversacion_id:
        await _fallar(db, "otra_conversacion", aprob.id, *actor, detalle={"conversacion": ctx.conversacion_id})
    try:
        huella_actual = huella_canonica(aprob.tipo_accion, contenido)
    except ErrorAprobacion:
        await _fallar(db, "huella_distinta", aprob.id, *actor)
    if huella_actual != aprob.huella:
        await _fallar(db, "huella_distinta", aprob.id, *actor, detalle={"huella_actual": huella_actual})
    if aprob.estado != "aprobada":
        await _fallar(db, _CODIGO_POR_ESTADO.get(aprob.estado, "estado_invalido"), aprob.id, *actor)
    if _aware(aprob.expira_en) <= momento:
        if await _transicion(db, aprob.id, ("aprobada",), "expirada"):
            _evento(db, aprob.id, "expirada", "aprobada", "expirada", "sistema", None)
        await db.commit()
        raise ErrorAprobacion("expirada")
    hecho = await _transicion(
        db,
        aprob.id,
        ("aprobada",),
        "ejecutando",
        Aprobacion.huella == huella_actual,
        Aprobacion.propietario_user_id == ctx.user_id,
        Aprobacion.conversacion_id == ctx.conversacion_id,
        ejecucion_iniciada_en=momento,
        task_id_ejecucion=ctx.task_id,
        attempt_id_ejecucion=ctx.attempt_id,
        lease_generation_ejecucion=ctx.lease_generation,
    )
    if not hecho:
        actual = await _cargar(db, aprob.id)
        await _fallar(db, _CODIGO_POR_ESTADO.get(actual.estado, "estado_invalido"), aprob.id, *actor)
    if not await _envio_cambiar(db, aprob.id, ("sin_envio",), "enviando", momento, incrementar=True):
        await _fallar(db, "ya_ejecutada", aprob.id, *actor)
    _evento(db, aprob.id, "ejecucion_iniciada", "aprobada", "ejecutando", *actor)
    await db.commit()
    return await _cargar(db, aprob.id)


async def reclamar_ejecucion_diferida(db, approval_id: str, ahora: datetime | None = None) -> Aprobacion:
    momento = _ahora(ahora)
    aprob = await _cargar(db, approval_id)
    if aprob is None:
        raise ErrorAprobacion("no_encontrada")
    actor = ("sistema", None, "servidor", None)
    if aprob.estado != "aprobada":
        await _fallar(db, _CODIGO_POR_ESTADO.get(aprob.estado, "estado_invalido"), aprob.id, *actor)
    if _aware(aprob.expira_en) <= momento:
        if await _transicion(db, aprob.id, ("aprobada",), "expirada"):
            _evento(db, aprob.id, "expirada", "aprobada", "expirada", *actor)
        await db.commit()
        raise ErrorAprobacion("expirada")
    hecho = await _transicion(
        db,
        aprob.id,
        ("aprobada",),
        "ejecutando",
        Aprobacion.huella == aprob.huella,
        ejecucion_iniciada_en=momento,
        task_id_ejecucion=None,
        attempt_id_ejecucion=None,
        lease_generation_ejecucion=None,
    )
    if not hecho:
        actual = await _cargar(db, aprob.id)
        await _fallar(db, _CODIGO_POR_ESTADO.get(actual.estado, "estado_invalido"), aprob.id, *actor)
    if not await _envio_cambiar(db, aprob.id, ("sin_envio",), "enviando", momento, incrementar=True):
        await _fallar(db, "ya_ejecutada", aprob.id, *actor)
    _evento(db, aprob.id, "ejecucion_iniciada", "aprobada", "ejecutando", *actor)
    await db.commit()
    return await _cargar(db, aprob.id)


async def _envio_cambiar(
    db,
    approval_id: str,
    desde: tuple[str, ...],
    hacia: str,
    ahora: datetime,
    proveedor_id: str | None = None,
    incrementar: bool = False,
) -> bool:
    valores = {"envio_estado": hacia, "envio_actualizado_en": ahora}
    if proveedor_id:
        valores["envio_proveedor_id"] = proveedor_id
    actual = await _cargar(db, approval_id)
    if actual is None or actual.envio_estado not in desde:
        return False
    if hacia not in ENVIO_TRANSICIONES.get(actual.envio_estado, set()):
        raise ErrorAprobacion("transicion_envio_invalida", f"{actual.envio_estado} -> {hacia}")
    if incrementar:
        valores["envio_intentos"] = (actual.envio_intentos or 0) + 1
    resultado = await db.execute(
        update(Aprobacion)
        .where(Aprobacion.id == approval_id, Aprobacion.envio_estado.in_(desde))
        .values(**valores)
        .execution_options(synchronize_session=False)
    )
    return resultado.rowcount == 1


async def registrar_resultado(
    db,
    approval_id: str,
    ctx: Contexto,
    clave: str,
    ok: bool,
    detalle: dict | None = None,
    ahora: datetime | None = None,
) -> Aprobacion:
    momento = _ahora(ahora)
    detalle = detalle or {}
    actor = ("agente", ctx.agent_id, ctx.canal, ctx.task_id)
    condiciones = (Aprobacion.idempotency_key == clave, Aprobacion.agent_id == ctx.agent_id)
    aprob = await _cargar(db, approval_id)
    if (
        aprob is None
        or aprob.estado != "ejecutando"
        or aprob.idempotency_key != clave
        or aprob.agent_id != ctx.agent_id
    ):
        await _fallar(db, "estado_invalido", approval_id if aprob else None, *actor)
    incierto = (not ok) and bool(detalle.get("incierto"))
    if incierto:
        await _envio_cambiar(db, approval_id, ("enviando",), "desconocida", momento)
        _evento(
            db,
            approval_id,
            "resultado_incierto",
            "ejecutando",
            "ejecutando",
            "agente",
            ctx.agent_id,
            ctx.canal,
            ctx.task_id,
            detalle,
        )
        await db.commit()
        return await _cargar(db, approval_id)
    if ok:
        proveedor = str(detalle.get("proveedor_id") or "") or None
        verificado = bool(detalle.get("confirmada_por_proveedor")) and bool(proveedor)
        envio_hacia = "confirmada" if verificado else "aceptada_proveedor"
        hacia_aprob = "ejecutada"
    else:
        envio_hacia = "fallida"
        hacia_aprob = "fallida"
        proveedor = None
    if not await _envio_cambiar(db, approval_id, ("enviando",), envio_hacia, momento, proveedor_id=proveedor):
        await _fallar(db, "estado_invalido", approval_id, *actor)
    hecho = await _transicion(
        db,
        approval_id,
        ("ejecutando",),
        hacia_aprob,
        *condiciones,
        ejecutado_en=momento,
        resultado_json={**detalle, "envio_estado": envio_hacia},
    )
    if not hecho:
        await _fallar(db, "estado_invalido", approval_id if await _cargar(db, approval_id) else None, *actor)
    _evento(
        db,
        approval_id,
        hacia_aprob,
        "ejecutando",
        hacia_aprob,
        "agente",
        ctx.agent_id,
        ctx.canal,
        ctx.task_id,
        {**detalle, "envio_estado": envio_hacia},
    )
    await db.commit()
    return await _cargar(db, approval_id)


async def conciliar_envio(db, approval_id: str, buscar_en_proveedor, ahora: datetime | None = None) -> Aprobacion:
    momento = _ahora(ahora)
    aprob = await _cargar(db, approval_id)
    if aprob is None:
        raise ErrorAprobacion("no_encontrada")
    if aprob.envio_estado not in ("desconocida", "aceptada_proveedor") or aprob.estado not in (
        "ejecutando",
        "ejecutada",
    ):
        raise ErrorAprobacion("no_conciliable", f"{aprob.estado}/{aprob.envio_estado}")
    encontrado = buscar_en_proveedor(aprob.huella)
    if encontrado:
        await _envio_cambiar(
            db, approval_id, ("desconocida", "aceptada_proveedor"), "confirmada", momento, proveedor_id=str(encontrado)
        )
        if aprob.estado == "ejecutando":
            await _transicion(db, approval_id, ("ejecutando",), "ejecutada", ejecutado_en=momento)
        _evento(
            db,
            approval_id,
            "conciliada_confirmada",
            aprob.estado,
            "ejecutada",
            "sistema",
            None,
            detalle={"proveedor_id": str(encontrado)},
        )
        await db.commit()
        return await _cargar(db, approval_id)
    if aprob.envio_estado == "aceptada_proveedor":
        await _envio_cambiar(db, approval_id, ("aceptada_proveedor",), "desconocida", momento)
        _evento(db, approval_id, "conciliada_no_encontrada", aprob.estado, aprob.estado, "sistema", None)
        await db.commit()
        return await _cargar(db, approval_id)
    referencia = aprob.envio_actualizado_en or aprob.ejecucion_iniciada_en
    from hermeshq.config import get_settings

    espera = timedelta(seconds=int(get_settings().approval_reconcile_wait_seconds))
    if referencia is not None and momento - _aware(referencia) < espera:
        raise ErrorAprobacion("conciliacion_prematura", "esperar antes de dar por no enviado")
    if not await _envio_cambiar(db, approval_id, ("desconocida",), "sin_envio", momento):
        raise ErrorAprobacion("no_conciliable", "el estado cambió durante la conciliación")
    await _transicion(db, approval_id, ("ejecutando",), "aprobada", ejecucion_iniciada_en=None)
    _evento(
        db,
        approval_id,
        "conciliada_no_encontrada",
        "ejecutando",
        "aprobada",
        "sistema",
        None,
        detalle={"huella": aprob.huella},
    )
    await db.commit()
    return await _cargar(db, approval_id)


def puede_reintentar_envio(aprob: Aprobacion) -> tuple[bool, str]:
    if aprob.envio_estado in ("aceptada_proveedor", "confirmada", "entregada"):
        return False, "ya aceptado por el proveedor: no reintentar"
    if aprob.envio_estado == "enviando":
        return False, "en curso"
    if aprob.envio_estado == "desconocida":
        return False, "estado desconocido: conciliar antes de reintentar"
    return True, "sin envío previo confirmado"


async def mantenimiento(db, ahora: datetime | None = None) -> dict:
    momento = _ahora(ahora)
    filas = (
        (await db.execute(select(Aprobacion).where(Aprobacion.estado.in_(("pendiente", "aprobada", "ejecutando")))))
        .scalars()
        .all()
    )
    cuenta = {"expiradas": 0, "inciertas": 0}
    for fila in filas:
        if fila.estado in ("pendiente", "aprobada") and _aware(fila.expira_en) <= momento:
            if await _transicion(db, fila.id, (fila.estado,), "expirada"):
                _evento(db, fila.id, "expirada", fila.estado, "expirada", "sistema", None)
                cuenta["expiradas"] += 1
        elif fila.estado == "ejecutando" and _aware(fila.ejecucion_iniciada_en) + TTL_EJECUCION_COLGADA <= momento:
            if fila.envio_estado == "enviando":
                await _envio_cambiar(db, fila.id, ("enviando",), "desconocida", momento)
                _evento(
                    db,
                    fila.id,
                    "colgada_incierta",
                    "ejecutando",
                    "ejecutando",
                    "sistema",
                    None,
                    detalle={"envio_previo": fila.envio_estado},
                )
                cuenta["inciertas"] += 1
    await db.commit()
    return cuenta


async def aprobadas_para(
    db, ctx: Contexto, tipo: str | None = None, ahora: datetime | None = None, estados: tuple[str, ...] = ("aprobada",)
) -> list[Aprobacion]:
    if ctx.no_atendida or not ctx.conversacion_id:
        return []
    momento = _ahora(ahora)
    condiciones = [
        Aprobacion.agent_id == ctx.agent_id,
        Aprobacion.propietario_user_id == ctx.user_id,
        Aprobacion.conversacion_id == ctx.conversacion_id,
        Aprobacion.estado.in_(estados),
    ]
    if tipo:
        condiciones.append(Aprobacion.tipo_accion == tipo)
    filas = (await db.execute(select(Aprobacion).where(*condiciones))).scalars().all()
    vigentes = [f for f in filas if f.estado == "ejecutada" or _aware(f.expira_en) > momento]
    return sorted(vigentes, key=lambda f: _aware(f.creado_en))


async def listar_para_usuario(db, user_id: str, estado: str | None = None) -> list[Aprobacion]:
    condiciones = [Aprobacion.propietario_user_id == user_id]
    if estado:
        condiciones.append(Aprobacion.estado == estado)
    filas = (await db.execute(select(Aprobacion).where(*condiciones))).scalars().all()
    return sorted(filas, key=lambda f: _aware(f.creado_en), reverse=True)


_PALABRAS_APROBACION = {
    "si",
    "sip",
    "ok",
    "okay",
    "dale",
    "listo",
    "confirmo",
    "confirmado",
    "adelante",
    "perfecto",
    "correcto",
    "envia",
    "envialo",
    "enviala",
    "enviar",
    "manda",
    "mandalo",
    "mandala",
    "autorizo",
    "apruebo",
    "aprobado",
    "procede",
    "hazlo",
    "yes",
    "reenvia",
    "reenvialo",
    "reenviala",
    "reenviale",
    "reenviar",
    "reenviarlo",
    "reenviarla",
    "responde",
    "respondelo",
    "respondele",
    "responder",
    "contesta",
    "contestalo",
    "contestale",
    "enviale",
    "mandale",
}
_PALABRAS_RELLENO = {
    "de",
    "acuerdo",
    "asi",
    "tal",
    "cual",
    "ya",
    "por",
    "favor",
    "porfa",
    "lo",
    "la",
    "el",
    "correo",
    "ahora",
    "con",
    "todo",
    "todos",
    "igual",
}


def es_aprobacion_textual(mensaje: str) -> bool:
    texto = re.sub(r"(?s)\[[^\]]*\]", " ", str(mensaje or "")).strip()
    if not texto or len(texto) > 60 or "?" in texto:
        return False
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    palabras = re.findall(r"[a-z]+", plano)
    if not palabras or not any(p in _PALABRAS_APROBACION for p in palabras):
        return False
    return all(p in _PALABRAS_APROBACION or p in _PALABRAS_RELLENO for p in palabras)


async def aprobar_por_texto(
    db, agent_id: str, user_id: str, conversacion_id: str, mensaje: str, canal: str, ahora: datetime | None = None
) -> Aprobacion | None:
    if not user_id or not conversacion_id or not es_aprobacion_textual(mensaje):
        return None
    momento = _ahora(ahora)
    filas = (
        (
            await db.execute(
                select(Aprobacion).where(
                    Aprobacion.agent_id == agent_id,
                    Aprobacion.propietario_user_id == user_id,
                    Aprobacion.conversacion_id == conversacion_id,
                    Aprobacion.estado == "pendiente",
                )
            )
        )
        .scalars()
        .all()
    )
    vigentes = [f for f in filas if _aware(f.expira_en) > momento]
    if len(vigentes) != 1:
        return None
    unica = vigentes[0]
    return await decidir(db, unica.id, True, user_id, f"{canal}:texto", unica.huella, conversacion_id, ahora=momento)


async def contexto_desde_tarea(
    db, agent_id: str, task_id: str, attempt_id: str | None = None, lease_generation: int | None = None
) -> Contexto:
    from hermeshq.models.task import Task
    from hermeshq.models.user import User
    from hermeshq.services.delegacion_entre_usuarios import es_cuenta_de_servicio

    tarea = await db.get(Task, task_id) if task_id else None
    if tarea is None or tarea.agent_id != agent_id or tarea.status != "running":
        raise ErrorAprobacion("tarea_invalida")
    meta = tarea.metadata_json or {}
    user_id = str(meta.get("thread_user_id") or tarea.created_by_user_id or "").strip()
    usuario = await db.get(User, user_id) if user_id else None
    if usuario is None or not usuario.is_active or es_cuenta_de_servicio(usuario.username):
        raise ErrorAprobacion("usuario_no_humano")
    conversacion = next(
        (str(meta.get(c)).strip() for c in ("thread_id", "public_session_id") if str(meta.get(c) or "").strip()), ""
    )
    return Contexto(
        agent_id=agent_id,
        user_id=user_id,
        conversacion_id=conversacion,
        task_id=tarea.id,
        canal=str(meta.get("source") or ""),
        no_atendida=bool(meta.get("scheduled") or meta.get("background_job")),
        attempt_id=attempt_id,
        lease_generation=lease_generation,
    )


MODOS_AGENTE = ("heredar", "activada", "desactivada")


OPERACIONES_ESCRITURA = (
    "correo.enviar",
    "correo.responder",
    "correo.reenviar",
    "calendario.crear",
    "calendario.modificar",
    "calendario.eliminar",
    "calendario.responder",
    "teams.mensaje",
    "onedrive.compartir",
)
MODOS_POLITICA = ("aprobacion", "bloqueada")
MODO_POR_DEFECTO = "aprobacion"


def operacion_de(tipo: str, contenido: dict | None) -> str:
    if tipo in (TIPO_CORREO, TIPO_RESPONDER, TIPO_REENVIAR):
        return tipo
    contenido = contenido or {}
    metodo = str(contenido.get("metodo") or "").upper()
    ruta = str(contenido.get("ruta") or "")
    if tipo == TIPO_TEAMS:
        return "teams.mensaje"
    if tipo == TIPO_ONEDRIVE:
        return "onedrive.compartir"
    if tipo == TIPO_CALENDARIO:
        if metodo == "POST" and ruta == "/me/events":
            return "calendario.crear"
        if metodo == "PATCH":
            return "calendario.modificar"
        if metodo == "DELETE":
            return "calendario.eliminar"
        return "calendario.responder"
    raise ErrorAprobacion("tipo_desconocido")


def _politica_valida(datos) -> dict[str, str]:
    if not isinstance(datos, dict):
        return {}
    return {k: v for k, v in datos.items() if k in OPERACIONES_ESCRITURA and v in MODOS_POLITICA}


def politica_tenant() -> dict[str, str]:
    from hermeshq.config import get_settings

    crudo = (get_settings().approval_policy_default or "").strip()
    if not crudo:
        return {}
    try:
        return _politica_valida(json.loads(crudo))
    except ValueError:
        return {}


def politica_efectiva(agente) -> dict[str, str]:
    tenant = politica_tenant()
    propia = _politica_valida(getattr(agente, "approval_policy", None))
    return {op: propia.get(op) or tenant.get(op) or MODO_POR_DEFECTO for op in OPERACIONES_ESCRITURA}


def exigir_operacion_permitida(agente, tipo: str, contenido: dict | None) -> str:
    operacion = operacion_de(tipo, contenido)
    if politica_efectiva(agente)[operacion] == "bloqueada":
        raise ErrorAprobacion(
            "operacion_bloqueada", f"La política del administrador no permite «{operacion}» a este agente."
        )
    return operacion


def central_activa_para(agente, ajustes=None) -> bool:
    if ajustes is None:
        from hermeshq.config import get_settings

        ajustes = get_settings()
    if ajustes.approval_central_enforced or not ajustes.m365_agent_token_enabled:
        return True
    modo = str(getattr(agente, "approval_central_mode", None) or "heredar")
    if modo == "activada":
        return True
    if modo == "desactivada":
        return False
    return bool(ajustes.approval_central_enabled)


async def central_activa_para_id(db, agent_id: str) -> bool:
    from hermeshq.models.agent import Agent

    agente = await db.get(Agent, agent_id)
    return agente is not None and central_activa_para(agente)


def respuesta_error(exc: ErrorAprobacion) -> dict:
    return {"ok": False, "codigo": exc.codigo, "error": exc.mensaje}


async def aprobar_texto_si_corresponde(
    db, agent_id: str, user_id: str, conversacion_id: str, mensaje: str, canal: str
) -> str | None:
    if not await central_activa_para_id(db, agent_id):
        return None
    try:
        aprob = await aprobar_por_texto(db, agent_id, user_id, conversacion_id, mensaje, canal)
    except ErrorAprobacion:
        return None
    return aprob.id if aprob else None
