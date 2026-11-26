from __future__ import annotations

import json
import re
import urllib.parse
from datetime import UTC, datetime, timedelta

from hermeshq.services import graph_proxy as gp

APLICADA = "aplicada"
NO_APLICADA = "no_aplicada"
INDETERMINADA = "indeterminada"
MARGEN = timedelta(minutes=5)
RESPUESTAS = {"accept": "accepted", "decline": "declined", "tentativelyAccept": "tentativelyAccepted"}


def transaction_id(aprobacion_id: str) -> str:
    return f"hermeshq-{aprobacion_id}"


def preparar_ejecucion(canon: dict, aprobacion_id: str) -> dict:
    if canon["metodo"] == "POST" and canon["ruta"] == "/me/events":
        return {**canon, "cuerpo": {**canon["cuerpo"], "transactionId": transaction_id(aprobacion_id)}}
    return canon


def _get(token: str, ruta: str, prefer: str | None = None) -> tuple[int, dict]:
    url = f"{gp.GRAPH_BASE}{ruta}"
    if prefer:
        estado, datos = gp._llamar_cabeceras("GET", url, token, {"Prefer": prefer}, 5_000_000)
    else:
        estado, datos = gp._llamar("GET", url, token, None, None, 5_000_000)
    try:
        return estado, json.loads(datos or b"{}")
    except ValueError:
        return estado, {}


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalizar(texto: str) -> str:
    from hermeshq.services.aprobacion_central import html_a_texto

    return re.sub(r"\s+", " ", html_a_texto(texto or "")).strip().lower()


def _fecha(valor) -> datetime | None:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", str(valor or ""))
    return datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC) if m else None


def _creado_desde(item: dict, desde: datetime) -> bool:
    creado = _fecha(item.get("createdDateTime"))
    return creado is None or creado >= desde - MARGEN


def _crear_evento(token: str, aprobacion_id: str, desde: datetime):
    consulta = urllib.parse.urlencode(
        {
            "$filter": f"createdDateTime ge {_iso(desde - MARGEN)}",
            "$select": "id,transactionId,createdDateTime",
            "$top": "100",
        },
        safe="$,: ",
    )
    estado, datos = _get(token, f"/me/events?{consulta}")
    if estado != 200:
        return INDETERMINADA, f"eventos_no_disponibles:{estado}"
    buscado = transaction_id(aprobacion_id)
    for evento in datos.get("value") or []:
        if evento.get("transactionId") == buscado:
            return APLICADA, str(evento.get("id") or buscado)
    if datos.get("@odata.nextLink"):
        return INDETERMINADA, "demasiados_eventos_recientes"
    return NO_APLICADA, None


def _igual(aprobado, remoto, clave: str = "") -> bool:
    if clave == "body" and isinstance(aprobado, dict):
        esperado = _normalizar(aprobado.get("content") or "")
        return esperado in _normalizar((remoto or {}).get("content") or "")
    if clave == "attendees" and isinstance(aprobado, list):

        def direcciones(lista):
            return sorted(str(((a or {}).get("emailAddress") or {}).get("address") or "").lower() for a in lista or [])

        return direcciones(aprobado) == direcciones(remoto)
    if clave == "dateTime":
        return str(aprobado)[:16] == str(remoto or "")[:16]
    if isinstance(aprobado, dict):
        if not isinstance(remoto, dict):
            return False
        return all(_igual(v, remoto.get(k), k) for k, v in aprobado.items())
    if isinstance(aprobado, str):
        return aprobado.strip() == str(remoto or "").strip()
    return aprobado == remoto


def _modificar_evento(token: str, ruta: str, cuerpo: dict):
    zona = ((cuerpo.get("start") or {}).get("timeZone")) or ((cuerpo.get("end") or {}).get("timeZone"))
    prefer = f'outlook.timezone="{zona}"' if zona else None
    estado, evento = _get(token, ruta, prefer)
    if estado != 200:
        return INDETERMINADA, f"evento_no_disponible:{estado}"
    return (APLICADA, str(evento.get("id") or ruta)) if _igual(cuerpo, evento) else (NO_APLICADA, None)


def _eliminar_evento(token: str, ruta: str):
    estado, evento = _get(token, f"{ruta}?$select=id,isCancelled")
    if estado == 404:
        return APLICADA, ruta.rsplit("/", 1)[-1]
    if estado == 200:
        return (APLICADA, str(evento.get("id"))) if evento.get("isCancelled") else (NO_APLICADA, None)
    return INDETERMINADA, f"evento_no_disponible:{estado}"


def _responder_evento(token: str, ruta: str, accion: str):
    base = ruta.rsplit("/", 1)[0]
    estado, evento = _get(token, f"{base}?$select=id,responseStatus")
    if estado == 404 and accion == "decline":
        return APLICADA, base.rsplit("/", 1)[-1]
    if estado != 200:
        return INDETERMINADA, f"evento_no_disponible:{estado}"
    respuesta = ((evento.get("responseStatus") or {}).get("response")) or ""
    return (APLICADA, str(evento.get("id"))) if respuesta == RESPUESTAS[accion] else (NO_APLICADA, None)


def _mensaje_teams(token: str, ruta: str, cuerpo: dict, desde: datetime, archivo: dict | None = None):
    from hermeshq.services import graph_central as gc

    directo = gc.destinatario_directo(ruta)
    if directo is not None:
        try:
            chat = gc.buscar_chat_directo(token, directo)
        except gc.AccionRechazada as exc:
            return INDETERMINADA, exc.motivo
        if chat is None:
            return NO_APLICADA, None
        ruta = f"/me/chats/{chat}/messages"
    estado, yo = _get(token, "/me?$select=id")
    if estado != 200 or not yo.get("id"):
        return INDETERMINADA, f"identidad_no_disponible:{estado}"
    estado, datos = _get(token, f"{ruta}?$top=50")
    if estado != 200:
        return INDETERMINADA, f"mensajes_no_disponibles:{estado}"
    esperado = _normalizar((cuerpo.get("body") or {}).get("content") or "")
    mensajes = datos.get("value") or []
    for m in mensajes:
        autor = (((m.get("from") or {}).get("user")) or {}).get("id")
        nombres = [str(a.get("name") or "") for a in m.get("attachments") or []]
        if (
            autor == yo["id"]
            and _creado_desde(m, desde)
            and _normalizar((m.get("body") or {}).get("content") or "") == esperado
            and (not archivo or any(n.endswith(archivo["nombre"]) for n in nombres))
        ):
            return APLICADA, str(m.get("id"))
    fechas = [f for f in (_fecha(m.get("createdDateTime")) for m in mensajes) if f is not None]
    cubre_la_ventana = len(mensajes) < 50 or bool(fechas and min(fechas) < desde - MARGEN)
    if datos.get("@odata.nextLink") and not cubre_la_ventana:
        return INDETERMINADA, "demasiados_mensajes_recientes"
    return NO_APLICADA, None


def _enlace_compartido(token: str, ruta: str, cuerpo: dict):
    item = ruta.rsplit("/", 1)[0]
    estado, datos = _get(token, f"{item}/permissions")
    if estado != 200:
        return INDETERMINADA, f"permisos_no_disponibles:{estado}"
    for permiso in datos.get("value") or []:
        enlace = permiso.get("link") or {}
        if enlace.get("scope") == cuerpo.get("scope") and enlace.get("type") == cuerpo.get("type"):
            return APLICADA, str(enlace.get("webUrl") or permiso.get("id"))
    return NO_APLICADA, None


def verificar(token: str, canon: dict, aprobacion_id: str, desde: datetime) -> tuple[str, str | None]:
    metodo, ruta, cuerpo = canon["metodo"], canon["ruta"], canon.get("cuerpo") or {}
    try:
        if metodo == "POST" and ruta == "/me/events":
            return _crear_evento(token, aprobacion_id, desde)
        if metodo == "PATCH" and re.match(r"^/me/events/[^/]+$", ruta):
            return _modificar_evento(token, ruta, cuerpo)
        if metodo == "DELETE" and re.match(r"^/me/events/[^/]+$", ruta):
            return _eliminar_evento(token, ruta)
        if metodo == "POST" and (m := re.match(r"^/me/events/[^/]+/(accept|decline|tentativelyAccept)$", ruta)):
            return _responder_evento(token, ruta, m.group(1))
        if metodo == "POST" and re.match(r"^/me/drive/items/[^/]+/createLink$", ruta):
            return _enlace_compartido(token, ruta, cuerpo)
        if metodo == "POST" and re.match(r"^/me/chats/[^/]+/messages$", ruta):
            from hermeshq.services import graph_central as gc

            persona = gc.destinatario_directo(ruta)
            if persona is not None:
                chat = gc.buscar_chat_directo(token, persona)
                if not chat:
                    return NO_APLICADA, None
                ruta = f"/me/chats/{chat}/messages"
            return _mensaje_teams(token, ruta, cuerpo, desde, canon.get("archivo"))
    except Exception as exc:  # noqa: BLE001
        return INDETERMINADA, f"consulta_fallida:{type(exc).__name__}"
    return INDETERMINADA, "operacion_sin_verificador"


RESOLUTOR_AUTOMATICO = "sistema:conciliacion_automatica"


async def cerrar_registros_externos(
    db, aprob_id: str, propietario: str, tipo: str, huella: str, envio_estado: str, resolutor: str, nota: str
) -> None:
    from sqlalchemy import text

    from hermeshq.services.external_actions import fingerprint_for, reconcile_external_action

    huella_accion = fingerprint_for(propietario, tipo, {"aprobacion": aprob_id, "huella": huella})
    filas = (
        await db.execute(
            text("SELECT id, task_id FROM external_action_records WHERE fingerprint = :f AND state = 'iniciada'"),
            {"f": huella_accion},
        )
    ).all()
    for fila in filas:
        if envio_estado == "confirmada":
            await reconcile_external_action(db, fila.task_id, fila.id, True, resolutor, nota)
        elif envio_estado == "sin_envio":
            await reconcile_external_action(db, fila.task_id, fila.id, False, resolutor, nota)


async def conciliar_automatico(db, aprob_id: str, token: str, resolutor: str = RESOLUTOR_AUTOMATICO):
    import asyncio

    from hermeshq.services import aprobacion_central as ac

    aprob = await ac._cargar(db, aprob_id)
    if aprob is None:
        raise ac.ErrorAprobacion("no_encontrada")
    desde = ac._aware(aprob.ejecucion_iniciada_en or aprob.envio_actualizado_en) or ac._ahora()
    canon, propietario, tipo, huella = (
        dict(aprob.contenido_json),
        aprob.propietario_user_id,
        aprob.tipo_accion,
        aprob.huella,
    )
    resultado, dato = await asyncio.to_thread(verificar, token, canon, aprob_id, desde)
    if resultado == INDETERMINADA:
        raise ac.ErrorAprobacion(
            "conciliacion_manual_requerida",
            f"No se pudo determinar si la acción se aplicó ({dato}). Revisa en Outlook/Teams e indica ejecutada=true o false.",
        )
    encontrado = dato if resultado == APLICADA else None
    final = await ac.conciliar_envio(db, aprob_id, lambda _huella: encontrado)
    nota = f"verificado en Microsoft 365: {dato}" if encontrado else "verificado en Microsoft 365: no aplicada"
    await cerrar_registros_externos(db, aprob_id, propietario, tipo, huella, final.envio_estado, resolutor, nota)
    return final, resultado, dato
