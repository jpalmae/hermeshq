from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from sqlalchemy import select

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.aprobacion import Aprobacion
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac
from hermeshq.services import conciliacion_graph as cg
from hermeshq.services import graph_central as gc
from hermeshq.services.delegacion_entre_usuarios import es_cuenta_de_servicio

logger = logging.getLogger(__name__)
ESPERA_ANTES_DEL_BARRIDO = timedelta(seconds=5)


def ejecutable_en_servidor(aprob: Aprobacion) -> bool:
    if aprob.tipo_accion not in ac.TIPOS_GRAPH:
        return False
    return not (aprob.tipo_accion == ac.TIPO_TEAMS and (aprob.contenido_json or {}).get("archivo"))


async def _token(db, usuario_id: str) -> str:
    from hermeshq.config import get_settings
    from hermeshq.services.m365_oauth import get_valid_token
    from hermeshq.services.secret_vault import build_vault_from_settings

    if os.environ.get("HERMESHQ_ENTORNO") == "aislado" and os.environ.get("HERMESHQ_GRAPH_TOKEN_SIMULADO"):
        return os.environ["HERMESHQ_GRAPH_TOKEN_SIMULADO"]
    token, _, _ = await get_valid_token(usuario_id, build_vault_from_settings(get_settings()), db)
    if not token:
        raise ac.ErrorAprobacion("sin_token_m365")
    return token


async def _motivo_bloqueo(db, aprob: Aprobacion) -> str | None:
    agente = await db.get(Agent, aprob.agent_id)
    if agente is None:
        return "agente_inexistente"
    if not ac.central_activa_para(agente):
        return "aprobacion_central_no_activa"
    try:
        ac.exigir_operacion_permitida(agente, aprob.tipo_accion, aprob.contenido_json)
    except ac.ErrorAprobacion as exc:
        return exc.codigo
    usuario = await db.get(User, aprob.propietario_user_id)
    if usuario is None or not usuario.is_active or es_cuenta_de_servicio(usuario.username):
        return "usuario_no_humano"
    asignacion = await db.execute(
        select(AgentAssignment.id).where(
            AgentAssignment.agent_id == agente.id, AgentAssignment.user_id == aprob.propietario_user_id
        )
    )
    if asignacion.scalar_one_or_none() is None:
        return "agente_no_asignado_al_usuario"
    return None


async def _expirar(db, aprob_id: str, motivo: str, momento) -> None:
    if await ac._transicion(db, aprob_id, ("aprobada",), "expirada"):
        ac._evento(
            db,
            aprob_id,
            "no_ejecutada_servidor",
            "aprobada",
            "expirada",
            "sistema",
            None,
            "servidor",
            None,
            {"motivo": motivo},
        )
        await db.commit()


def _miembros_cambiaron(token: str, aprob: Aprobacion) -> str | None:
    try:
        actuales = gc.miembros_del_chat(token, str((aprob.contenido_json or {}).get("ruta") or ""))
    except gc.AccionRechazada as exc:
        return f"miembros_no_verificables:{exc.motivo}"
    except Exception as exc:  # noqa: BLE001
        return f"miembros_no_verificables:{type(exc).__name__}"
    esperados = sorted(str(m).strip().lower() for m in (aprob.contenido_json or {}).get("miembros") or [])
    return None if sorted(str(m).strip().lower() for m in actuales) == esperados else "miembros_cambiaron"


def _resumen_respuesta(respuesta) -> dict:
    if not isinstance(respuesta, dict):
        return {}
    resumen = {k: respuesta[k] for k in ("id", "subject", "webLink") if isinstance(respuesta.get(k), str)}
    enlace = respuesta.get("link")
    if isinstance(enlace, dict) and isinstance(enlace.get("webUrl"), str):
        resumen["enlace"] = enlace["webUrl"]
    return resumen


async def _registrar(db, aprob: Aprobacion, ok: bool, detalle: dict, momento) -> Aprobacion:
    ctx = ac.Contexto(
        agent_id=aprob.agent_id,
        user_id=aprob.propietario_user_id,
        conversacion_id=aprob.conversacion_id,
        canal="servidor",
    )
    await ac.registrar_resultado(db, aprob.id, ctx, aprob.idempotency_key, ok, detalle, momento)
    return await ac._cargar(db, aprob.id)


async def _ejecutar_reclamada(db, aprob: Aprobacion, token: str, momento) -> Aprobacion:
    canon = dict(aprob.contenido_json)
    try:
        estado_graph, respuesta = await asyncio.to_thread(
            gc.ejecutar, token, cg.preparar_ejecucion(canon, aprob.id), aprob.id, None
        )
    except gc.AccionRechazada as exc:
        return await _registrar(db, aprob, False, {"paso": "preparacion", "motivo": exc.motivo}, momento)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ejecución servidor incierta: aprobacion=%s error=%s", aprob.id, type(exc).__name__)
        return await _registrar(db, aprob, False, {"incierto": True, "error": type(exc).__name__}, momento)
    if 200 <= estado_graph < 300:
        proveedor = str(respuesta.get("id") or "") if isinstance(respuesta, dict) else ""
        return await _registrar(
            db,
            aprob,
            True,
            {
                "status": estado_graph,
                "proveedor_id": proveedor or None,
                "confirmada_por_proveedor": bool(proveedor),
                "respuesta": _resumen_respuesta(respuesta),
            },
            momento,
        )
    if 400 <= estado_graph < 500:
        error = (respuesta.get("error") or {}).get("message") if isinstance(respuesta, dict) else None
        return await _registrar(db, aprob, False, {"status": estado_graph, "error": error}, momento)
    return await _registrar(db, aprob, False, {"incierto": True, "status": estado_graph}, momento)


async def ejecutar(db, approval_id: str, ahora=None) -> Aprobacion | None:
    momento = ac._ahora(ahora)
    aprob = await ac._cargar(db, approval_id)
    if aprob is None or aprob.estado != "aprobada" or not ejecutable_en_servidor(aprob):
        return aprob
    if ac._aware(aprob.expira_en) <= momento:
        await _expirar(db, aprob.id, "expirada", momento)
        return await ac._cargar(db, approval_id)
    motivo = await _motivo_bloqueo(db, aprob)
    if motivo:
        await _expirar(db, aprob.id, motivo, momento)
        return await ac._cargar(db, approval_id)
    try:
        token = await _token(db, aprob.propietario_user_id)
    except Exception as exc:  # noqa: BLE001
        motivo = exc.codigo if isinstance(exc, ac.ErrorAprobacion) else "sin_token_m365"
        await _expirar(db, aprob.id, motivo, momento)
        return await ac._cargar(db, approval_id)
    if aprob.tipo_accion == ac.TIPO_TEAMS:
        motivo = await asyncio.to_thread(_miembros_cambiaron, token, aprob)
        if motivo:
            await _expirar(db, aprob.id, motivo, momento)
            return await ac._cargar(db, approval_id)
    try:
        reclamada = await ac.reclamar_ejecucion_diferida(db, aprob.id, momento)
    except ac.ErrorAprobacion:
        return await ac._cargar(db, approval_id)
    return await _ejecutar_reclamada(db, reclamada, token, momento)


async def _conciliar_incierta(db, aprob_id: str, momento) -> None:
    aprob = await ac._cargar(db, aprob_id)
    if aprob is None or aprob.envio_estado != "desconocida":
        return
    try:
        token = await _token(db, aprob.propietario_user_id)
    except Exception:  # noqa: BLE001
        return
    try:
        final, _resultado, _dato = await cg.conciliar_automatico(db, aprob_id, token)
    except ac.ErrorAprobacion as exc:
        logger.info("conciliación pendiente: aprobacion=%s motivo=%s", aprob_id, exc.codigo)
        return
    if final.estado == "aprobada":
        await ejecutar(db, aprob_id, momento)


async def barrido(session_factory, ahora=None) -> dict:
    momento = ac._ahora(ahora)
    cuenta = {"ejecutadas": 0, "conciliadas": 0, "expiradas": 0}
    async with session_factory() as db:
        cuenta["expiradas"] = (await ac.mantenimiento(db, momento))["expiradas"]
    async with session_factory() as db:
        pendientes = (
            await db.execute(
                select(Aprobacion.id, Aprobacion.tipo_accion, Aprobacion.contenido_json).where(
                    Aprobacion.estado == "aprobada",
                    Aprobacion.envio_estado == "sin_envio",
                    Aprobacion.decidido_en <= momento - ESPERA_ANTES_DEL_BARRIDO,
                )
            )
        ).all()
        inciertas = (
            (
                await db.execute(
                    select(Aprobacion.id).where(
                        Aprobacion.estado == "ejecutando", Aprobacion.envio_estado == "desconocida"
                    )
                )
            )
            .scalars()
            .all()
        )
    for fila in pendientes:
        if fila.tipo_accion not in ac.TIPOS_GRAPH:
            continue
        async with session_factory() as db:
            try:
                if (await ejecutar(db, fila.id, momento)) and (await ac._cargar(db, fila.id)).estado == "ejecutada":
                    cuenta["ejecutadas"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("barrido: no se pudo ejecutar aprobacion=%s", fila.id)
    for aprob_id in inciertas:
        async with session_factory() as db:
            try:
                await _conciliar_incierta(db, aprob_id, momento)
                cuenta["conciliadas"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("barrido: conciliación fallida aprobacion=%s", aprob_id)
    return cuenta


MARCA_EJECUTADA = "SÍ se ejecutó"

_NOMBRE_TIPO = {
    ac.TIPO_CALENDARIO: "calendario",
    ac.TIPO_TEAMS: "Teams",
    ac.TIPO_ONEDRIVE: "compartir archivo",
}


def aviso_para_agente(aprob: Aprobacion | None) -> str | None:
    if aprob is None or aprob.tipo_accion not in ac.TIPOS_GRAPH:
        return None
    nombre = _NOMBRE_TIPO.get(aprob.tipo_accion, "acción")
    datos = aprob.resultado_json or {}
    if aprob.estado == "ejecutada":
        respuesta = datos.get("respuesta") or {}
        detalle = respuesta.get("subject") or respuesta.get("enlace") or respuesta.get("id") or ""
        return (
            f"La acción aprobada de {nombre} {MARCA_EJECUTADA} en Microsoft 365"
            + (f" ({detalle})" if detalle else "")
            + ". No la repitas."
        )
    if aprob.estado == "fallida":
        return (
            f"La acción aprobada de {nombre} NO se ejecutó: {_motivo_fallo(datos)}. No la repitas: "
            "para intentarlo hay que pedir una aprobación nueva."
        )
    if aprob.estado == "ejecutando" or aprob.envio_estado == "desconocida":
        return (
            f"La acción aprobada de {nombre} quedó con resultado incierto y NO se ha repetido. "
            "No la vuelvas a ejecutar ni afirmes que se hizo."
        )
    return None


def _motivo_fallo(datos: dict) -> str:
    if datos.get("motivo"):
        return f"se paró en {datos.get('paso') or 'la ejecución'} ({datos['motivo']})"
    if datos.get("status"):
        return f"Microsoft 365 respondió {datos['status']}"
    return "Microsoft 365 rechazó la acción"


async def aviso_de_aprobacion(db, aprobacion_id: str | None) -> str | None:
    if not aprobacion_id:
        return None
    return aviso_para_agente(await ac._cargar(db, aprobacion_id))


async def avisos_pendientes(db, agent_id: str, user_id: str, conversacion_id: str | None) -> str | None:
    if not conversacion_id or not user_id:
        return None
    from sqlalchemy import exists, select

    from hermeshq.models.aprobacion import EventoAprobacion

    entregado = exists().where(
        EventoAprobacion.approval_id == Aprobacion.id, EventoAprobacion.evento == "aviso_entregado"
    )
    filas = (
        (
            await db.execute(
                select(Aprobacion)
                .where(
                    Aprobacion.agent_id == agent_id,
                    Aprobacion.propietario_user_id == user_id,
                    Aprobacion.conversacion_id == conversacion_id,
                    Aprobacion.tipo_accion.in_(ac.TIPOS_GRAPH),
                    Aprobacion.estado.in_(("ejecutada", "fallida")),
                    ~entregado,
                )
                .order_by(Aprobacion.creado_en)
            )
        )
        .scalars()
        .all()
    )
    avisos = []
    for aprob in filas:
        texto = aviso_para_agente(aprob)
        if texto:
            avisos.append(texto)
            ac._evento(db, aprob.id, "aviso_entregado", aprob.estado, aprob.estado, "sistema", None, "servidor", None)
    return " ".join(avisos) if avisos else None
