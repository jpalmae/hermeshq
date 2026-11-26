from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac
from hermeshq.services.credencial_agente import credencial_valida

logger = logging.getLogger(__name__)

HTTP_POR_CODIGO = {
    "no_encontrada": 404,
    "otro_usuario": 404,
    "otra_conversacion": 409,
    "huella_distinta": 409,
    "estado_invalido": 409,
    "rechazada": 409,
    "ya_ejecutada": 409,
    "expirada": 410,
    "no_atendida": 403,
    "usuario_no_humano": 403,
    "pendiente": 409,
    "fallida": 409,
    "tarea_invalida": 403,
    "no_conciliable": 409,
    "conciliacion_prematura": 409,
    "conciliacion_manual_requerida": 409,
    "operacion_bloqueada": 403,
}


def _http(exc: ac.ErrorAprobacion) -> HTTPException:
    return HTTPException(
        status_code=HTTP_POR_CODIGO.get(exc.codigo, 400), detail={"codigo": exc.codigo, "error": exc.mensaje}
    )


CODIGO_BRIDGE = {
    "rechazada": "ya_decidida",
    "estado_invalido": "ya_decidida",
    "ya_ejecutada": "ya_decidida",
    "pendiente": "ya_decidida",
    "fallida": "ya_decidida",
}


def _error_bridge(status: int, code: str, mensaje: str, codigo: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"codigo": codigo or code, "code": code, "error": mensaje})


def _http_bridge(exc: ac.ErrorAprobacion) -> HTTPException:
    return _error_bridge(
        HTTP_POR_CODIGO.get(exc.codigo, 400), CODIGO_BRIDGE.get(exc.codigo, exc.codigo), exc.mensaje, exc.codigo
    )


user_router = APIRouter(prefix="/aprobaciones", tags=["aprobaciones"])
internal_router = APIRouter(prefix="/internal/control/aprobaciones", include_in_schema=False)
public_router = APIRouter(prefix="/api/public/chat/aprobaciones")


class DecisionWeb(BaseModel):
    huella: str = Field(min_length=64, max_length=64)
    conversacion_id: str = Field(min_length=1, max_length=255)


@user_router.get("/panel", include_in_schema=False, response_class=HTMLResponse)
async def panel() -> HTMLResponse:
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "assets" / "panel_aprobaciones.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'self'",
            "Cache-Control": "no-store",
        },
    )


@user_router.get("")
async def listar(
    estado: str | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db_session)
) -> list[dict]:
    return [ac.resumen(a) for a in await ac.listar_para_usuario(db, user.id, estado)]


@user_router.get("/{approval_id}")
async def ver(
    approval_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db_session)
) -> dict:
    try:
        return ac.resumen(await ac.ver(db, approval_id, user.id))
    except ac.ErrorAprobacion as exc:
        raise _http(exc)


async def _decidir_web(approval_id: str, aprobar: bool, cuerpo: DecisionWeb, user: User, db: AsyncSession) -> dict:
    try:
        aprob = await ac.decidir(db, approval_id, aprobar, user.id, "web", cuerpo.huella, cuerpo.conversacion_id)
    except ac.ErrorAprobacion as exc:
        raise _http(exc)
    return ac.resumen(aprob)


@user_router.post("/{approval_id}/aprobar")
async def aprobar(
    approval_id: str,
    cuerpo: DecisionWeb,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await _decidir_web(approval_id, True, cuerpo, user, db)


@user_router.post("/{approval_id}/rechazar")
async def rechazar(
    approval_id: str,
    cuerpo: DecisionWeb,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await _decidir_web(approval_id, False, cuerpo, user, db)


class ConciliacionManual(BaseModel):
    ejecutada: bool


@user_router.post("/{approval_id}/conciliar")
async def conciliar(
    approval_id: str,
    cuerpo: ConciliacionManual | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    import asyncio

    from sqlalchemy import text

    from hermeshq.routers.m365_broker import token_del_servidor
    from hermeshq.services import correo_central as cc
    from hermeshq.services.external_actions import fingerprint_for, reconcile_external_action

    try:
        aprob = await ac.ver(db, approval_id, user.id)
    except ac.ErrorAprobacion as exc:
        raise _http(exc)
    aprob_id, huella, propietario = aprob.id, aprob.huella, aprob.propietario_user_id
    tipo_accion = aprob.tipo_accion
    if tipo_accion in ac.TIPOS_GRAPH:
        from hermeshq.services import conciliacion_graph as cg

        if cuerpo is None:
            token = await token_del_servidor(db, propietario)
            try:
                final, resultado, dato = await cg.conciliar_automatico(db, aprob_id, token)
            except ac.ErrorAprobacion as exc:
                raise _http(exc)
            encontrado = dato if resultado == cg.APLICADA else None
        else:
            encontrado = f"manual:{user.id}" if cuerpo.ejecutada else None
            try:
                final = await ac.conciliar_envio(db, aprob_id, lambda _huella: encontrado)
            except ac.ErrorAprobacion as exc:
                raise _http(exc)
            await cg.cerrar_registros_externos(
                db,
                aprob_id,
                propietario,
                tipo_accion,
                huella,
                final.envio_estado,
                user.id,
                "confirmada por el usuario" if encontrado else "el usuario indicó que no se aplicó",
            )
        estado_envio = final.envio_estado
    else:
        token = await token_del_servidor(db, propietario)
        try:
            encontrado = await asyncio.to_thread(cc.buscar_en_enviados, token, aprob_id)
        except cc.EnvioRechazado as exc:
            raise HTTPException(status_code=502, detail={"codigo": exc.motivo, "error": "No se pudo revisar Enviados."})
        try:
            final = await ac.conciliar_envio(db, aprob_id, lambda _huella: encontrado)
        except ac.ErrorAprobacion as exc:
            raise _http(exc)
        estado_envio = final.envio_estado
        huella_accion = fingerprint_for(propietario, tipo_accion, {"aprobacion": aprob_id, "huella": huella})
        filas = (
            await db.execute(
                text("SELECT id, task_id FROM external_action_records WHERE fingerprint = :f AND state = 'iniciada'"),
                {"f": huella_accion},
            )
        ).all()
        for fila in filas:
            if estado_envio == "confirmada":
                await reconcile_external_action(
                    db, fila.task_id, fila.id, True, user.id, f"encontrado en Enviados: {encontrado}"
                )
            elif estado_envio == "sin_envio":
                await reconcile_external_action(db, fila.task_id, fila.id, False, user.id, "no está en Enviados")
    return {
        "ok": True,
        "estado": final.estado,
        "envio_estado": estado_envio,
        "encontrado": bool(encontrado),
        "mensaje": ac.FRASE_ENVIO.get(estado_envio, estado_envio),
    }


async def agente_autenticado(
    db: AsyncSession = Depends(get_db_session),
    agent_id: str = Header(default="", alias="X-HermesHQ-Agent-ID"),
    agent_token: str = Header(default="", alias="X-HermesHQ-Agent-Token"),
) -> Agent:
    if not await credencial_valida(db, agent_id, agent_token, origen="aprobaciones"):
        raise HTTPException(status_code=401, detail="Invalid agent credentials")
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=401, detail="Unknown agent")
    return agent


class CrearInterno(BaseModel):
    task_id: str
    tipo: str
    contenido: dict
    parametros: dict | None = None


async def _intento(db, headers, agent_id: str, task_id: str):
    from hermeshq.services.external_actions import ExternalActionDenied, require_live_attempt

    try:
        return await require_live_attempt(db, headers, agent_id=agent_id, task_id=task_id)
    except ExternalActionDenied as exc:
        raise HTTPException(status_code=403, detail={"codigo": exc.motivo, "error": "El intento no está vigente."})


async def _con_miembros(db, agent_id: str, task_id: str, contenido: dict) -> dict:
    import asyncio

    from hermeshq.models.task import Task
    from hermeshq.routers.m365_broker import token_del_servidor
    from hermeshq.services import graph_central as gc
    from hermeshq.services.task_visibility import task_audience_user_id

    tarea = await db.get(Task, task_id)
    usuario = task_audience_user_id(tarea) if tarea else None
    if not usuario or not isinstance(contenido, dict):
        raise HTTPException(status_code=403, detail={"codigo": "tarea_invalida"})
    token = await token_del_servidor(db, usuario)
    try:
        miembros = await asyncio.to_thread(gc.miembros_del_chat, token, str(contenido.get("ruta") or "/me/chats/x/y"))
    except gc.AccionRechazada as exc:
        raise HTTPException(status_code=409, detail={"codigo": exc.motivo, "error": "No se pudo resolver el chat."})
    return {**contenido, "miembros": miembros}


async def _con_archivo(db, task_id: str, contenido: dict) -> dict:
    import asyncio

    from hermeshq.models.task import Task
    from hermeshq.routers.m365_broker import token_del_servidor
    from hermeshq.services import graph_central as gc
    from hermeshq.services.task_visibility import task_audience_user_id

    tarea = await db.get(Task, task_id)
    usuario = task_audience_user_id(tarea) if tarea else None
    if not usuario or not isinstance(contenido, dict):
        raise HTTPException(status_code=403, detail={"codigo": "tarea_invalida"})
    token = await token_del_servidor(db, usuario)
    try:
        archivo = await asyncio.to_thread(gc.archivo_de_item, token, str(contenido.get("ruta") or ""))
    except gc.AccionRechazada as exc:
        raise HTTPException(status_code=409, detail={"codigo": exc.motivo, "error": "No se pudo resolver el archivo."})
    return {**contenido, "archivo": archivo}


async def api_crear(db, agent_id: str, cuerpo: CrearInterno, headers=None) -> dict:
    agente = await db.get(Agent, agent_id)
    if agente is None or not ac.central_activa_para(agente):
        raise HTTPException(status_code=404, detail={"codigo": "aprobacion_central_no_activa"})
    try:
        ac.exigir_operacion_permitida(agente, cuerpo.tipo, cuerpo.contenido)
    except ac.ErrorAprobacion as exc:
        return ac.respuesta_error(exc)
    ref = await _intento(db, headers or {}, agent_id, cuerpo.task_id)
    if cuerpo.tipo == ac.TIPO_TEAMS:
        cuerpo.contenido = await _con_miembros(db, agent_id, cuerpo.task_id, cuerpo.contenido)
    if cuerpo.tipo == ac.TIPO_ONEDRIVE:
        cuerpo.contenido = await _con_archivo(db, cuerpo.task_id, cuerpo.contenido)
    try:
        ctx = await ac.contexto_desde_tarea(db, agent_id, cuerpo.task_id, ref.attempt_id, ref.generation)
        aprob = await ac.crear(db, cuerpo.tipo, cuerpo.contenido, ctx, cuerpo.parametros)
    except ac.ErrorAprobacion as exc:
        return ac.respuesta_error(exc)
    return {"ok": True, **ac.resumen(aprob), "tarjeta": ac.tarjeta_adaptativa(aprob)}


async def api_listas(db, agent_id: str, task_id: str, tipo: str | None, headers=None) -> dict:
    await _intento(db, headers or {}, agent_id, task_id)
    try:
        ctx = await ac.contexto_desde_tarea(db, agent_id, task_id)
    except ac.ErrorAprobacion as exc:
        return ac.respuesta_error(exc)
    estados = ("aprobada", "ejecutada") if tipo == ac.TIPO_ONEDRIVE else ("aprobada",)
    filas = await ac.aprobadas_para(db, ctx, tipo, estados=estados)
    return {"ok": True, "aprobadas": [{**ac.resumen(a), "parametros": a.parametros_json} for a in filas]}


@internal_router.post("")
async def interno_crear(
    cuerpo: CrearInterno,
    request: Request,
    agent: Agent = Depends(agente_autenticado),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await api_crear(db, agent.id, cuerpo, request.headers)


@internal_router.get("/aprobadas")
async def interno_listas(
    request: Request,
    task_id: str,
    tipo: str | None = None,
    agent: Agent = Depends(agente_autenticado),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await api_listas(db, agent.id, task_id, tipo, request.headers)


@internal_router.get("/{approval_id}")
async def interno_ver(
    approval_id: str,
    request: Request,
    task_id: str,
    agent: Agent = Depends(agente_autenticado),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await _intento(db, request.headers, agent.id, task_id)
    try:
        ctx = await ac.contexto_desde_tarea(db, agent.id, task_id)
    except ac.ErrorAprobacion as exc:
        return ac.respuesta_error(exc)
    aprob = await ac._cargar(db, approval_id)
    if (
        aprob is None
        or aprob.agent_id != agent.id
        or aprob.propietario_user_id != ctx.user_id
        or aprob.conversacion_id != ctx.conversacion_id
    ):
        return ac.respuesta_error(ac.ErrorAprobacion("no_encontrada"))
    return {"ok": True, **ac.resumen(aprob)}


class DecisionBridge(BaseModel):
    session_id: str
    session_token: str
    decision: str = Field(pattern="^(aprobar|rechazar)$")
    huella: str = Field(min_length=64, max_length=64)
    aad_object_id: str = Field(min_length=1, max_length=255)
    tenant_id: str | None = None
    activity_id: str | None = None


async def api_decision_bridge(
    service,
    db,
    api_key_header: str,
    approval_id: str,
    cuerpo: DecisionBridge,
    origin: str | None = None,
    request: Request | None = None,
) -> dict:
    from hermeshq.models.public_chat import PublicChatApiKey
    from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID

    try:
        api_key = await service.validate_api_key(api_key_header, db, origin=origin)
    except ValueError as exc:
        raise _error_bridge(401, "clave_invalida", str(exc))
    if request is not None:
        from hermeshq.routers.public_chat import validar_canal

        await validar_canal(request, db, api_key)
    try:
        session = await service.validate_session(cuerpo.session_id, cuerpo.session_token, db)
    except ValueError as exc:
        raise _error_bridge(401, "sesion_caducada", str(exc))
    if session.api_key_id != api_key.id:
        raise _error_bridge(403, "otra_conversacion", "La sesión no pertenece a esta clave")
    clave = await db.get(PublicChatApiKey, session.api_key_id)
    user_id, _ = await service._resolve_identity(clave, session, db)
    if not user_id or user_id == PUBLIC_CHAT_USER_ID:
        if clave.trust_sender_id and not hmac.compare_digest(str(session.sender_id or ""), cuerpo.aad_object_id):
            raise _error_bridge(403, "otro_usuario", "Quien pulsó la tarjeta no es el dueño de la sesión")
        raise _error_bridge(403, "usuario_no_humano", ac.MENSAJES["usuario_no_humano"])
    problema = await verificar_quien_pulsa(db, clave, session, user_id, cuerpo.aad_object_id, cuerpo.tenant_id)
    if problema:
        raise _error_bridge(403, "otro_usuario", problema)
    try:
        aprob = await ac.decidir(
            db,
            approval_id,
            cuerpo.decision == "aprobar",
            user_id,
            "teams",
            cuerpo.huella,
            session.id,
            actor_tipo="usuario",
        )
    except ac.ErrorAprobacion as exc:
        raise _http_bridge(exc)
    return {"ok": True, "estado": aprob.estado, "id": aprob.id, "tarjeta": ac.tarjeta_decidida(aprob)}


async def verificar_quien_pulsa(
    db, clave, session, user_id: str, aad_object_id: str, tenant_id: str | None
) -> str | None:
    registrados = await aad_registrados(db, clave, session, user_id)
    aad = (aad_object_id or "").strip()
    if registrados:
        if any(hmac.compare_digest(aad, valor) for valor in registrados):
            return None
        return "Quien pulsó la tarjeta no es el dueño de la sesión"
    if clave.trust_sender_id:
        if hmac.compare_digest(str(session.sender_id or ""), aad):
            return None
        return "Quien pulsó la tarjeta no es el dueño de la sesión"
    logger.info(
        "aprobacion_aad_sin_identidad: clave %s usuario %s aad %s aceptado sin identidad registrada",
        clave.id,
        user_id,
        aad,
    )
    return None


async def aad_registrados(db, clave, session, user_id: str) -> list[str]:
    from hermeshq.services.canales_bot import id_canal_efectivo, identidad_en_canal, identidades_de_usuario

    canal_id, _ = await id_canal_efectivo(db, clave)
    identidad = identidad_en_canal(await identidades_de_usuario(db, user_id), canal_id)
    valores = [identidad.aad_object_id] if identidad is not None and identidad.aad_object_id else []
    if not valores and clave.trust_sender_id and getattr(session, "aad_object_id", None):
        valores = [session.aad_object_id]
    return [v.strip() for v in valores if v and v.strip()]


@public_router.get("/pendientes")
async def bridge_pendientes(
    request: Request,
    session_id: str,
    session_token: str,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from hermeshq.models.public_chat import PublicChatApiKey
    from hermeshq.routers.public_chat import _get_service
    from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID

    service = _get_service(request)
    try:
        api_key = await service.validate_api_key(x_api_key, db, origin=request.headers.get("origin"))
        session = await service.validate_session(session_id, session_token, db)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if session.api_key_id != api_key.id:
        raise HTTPException(status_code=403, detail="La sesión no pertenece a esta clave")
    from hermeshq.routers.public_chat import validar_canal

    await validar_canal(request, db, api_key)
    clave = await db.get(PublicChatApiKey, session.api_key_id)
    user_id, _ = await service._resolve_identity(clave, session, db)
    if not user_id or user_id == PUBLIC_CHAT_USER_ID:
        return {"ok": True, "aprobaciones": []}
    filas = [
        a
        for a in await ac.listar_para_usuario(db, user_id, "pendiente")
        if a.agent_id == session.agent_id and a.conversacion_id == session.id
    ]
    return {
        "ok": True,
        "aprobaciones": [{"id": a.id, "huella": a.huella, "tarjeta": ac.tarjeta_adaptativa(a)} for a in filas],
    }


@public_router.post("/{approval_id}/decision")
async def bridge_decision(
    approval_id: str,
    cuerpo: DecisionBridge,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from hermeshq.routers.public_chat import _get_service

    return await api_decision_bridge(
        _get_service(request), db, x_api_key, approval_id, cuerpo, request.headers.get("origin"), request
    )


admin_router = APIRouter(prefix="/agents", tags=["aprobaciones"])


class ModoAprobacion(BaseModel):
    modo: str = Field(pattern="^(heredar|activada|desactivada)$")


@admin_router.get("/{agent_id}/aprobacion-central")
async def ver_modo(
    agent_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
) -> dict:
    agente = await db.get(Agent, agent_id)
    if agente is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "agent_id": agent_id,
        "modo": agente.approval_central_mode,
        "efectiva": ac.central_activa_para(agente),
        "politica_agente": agente.approval_policy or {},
        "politica_tenant": ac.politica_tenant(),
        "politica_efectiva": ac.politica_efectiva(agente),
        "lecturas": "sin aprobación humana (según la política de lectura m365_graph del agente)",
        "politica_m365_origen": _origen_politica_m365(agente),
    }


def _origen_politica_m365(agente) -> str:
    from hermeshq.services import graph_proxy as gp

    return gp.politica_de_agente(agente.integration_configs)[1]


class PoliticaPorOperacion(BaseModel):
    politica: dict[str, str | None]


@admin_router.put("/{agent_id}/aprobacion-central/politica")
async def cambiar_politica(
    agent_id: str,
    cuerpo: PoliticaPorOperacion,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from sqlalchemy import select

    from hermeshq.models.activity import ActivityLog
    from hermeshq.models.aprobacion import Aprobacion

    desconocidas = sorted(set(cuerpo.politica) - set(ac.OPERACIONES_ESCRITURA))
    invalidas = sorted(k for k, v in cuerpo.politica.items() if v is not None and v not in ac.MODOS_POLITICA)
    if desconocidas or invalidas:
        raise HTTPException(
            status_code=422,
            detail={
                "codigo": "politica_invalida",
                "operaciones_desconocidas": desconocidas,
                "modos_invalidos": invalidas,
                "operaciones": list(ac.OPERACIONES_ESCRITURA),
                "modos": list(ac.MODOS_POLITICA),
            },
        )
    agente = await db.get(Agent, agent_id)
    if agente is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    antes = ac.politica_efectiva(agente)
    anterior = dict(agente.approval_policy or {})
    nueva = {**anterior, **cuerpo.politica}
    nueva = {k: v for k, v in nueva.items() if v is not None}
    agente.approval_policy = nueva or None
    despues = ac.politica_efectiva(agente)
    bloqueadas = {op for op, modo in despues.items() if modo == "bloqueada" and antes[op] != "bloqueada"}
    expiradas: list[str] = []
    if bloqueadas:
        abiertas = (
            (
                await db.execute(
                    select(Aprobacion).where(
                        Aprobacion.agent_id == agent_id,
                        Aprobacion.estado.in_(("pendiente", "aprobada", "ejecutando")),
                    )
                )
            )
            .scalars()
            .all()
        )
        afectadas = [a for a in abiertas if ac.operacion_de(a.tipo_accion, a.contenido_json) in bloqueadas]
        if any(a.estado == "ejecutando" for a in afectadas):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "codigo": "ejecucion_en_curso",
                    "error": "Hay una operación de ese tipo en curso o sin conciliar; concíliala antes de bloquear.",
                },
            )
        for aprob in afectadas:
            if await ac._transicion(db, aprob.id, (aprob.estado,), "expirada"):
                ac._evento(db, aprob.id, "politica_cambiada", aprob.estado, "expirada", "usuario", admin.id)
                expiradas.append(aprob.id)
    db.add(
        ActivityLog(
            agent_id=agent_id,
            event_type="agent.approval_policy_changed",
            message="Política de aprobación por operación actualizada",
            details={"por": admin.id, "anterior": anterior, "nueva": nueva, "expiradas": expiradas},
        )
    )
    await db.commit()
    return {"agent_id": agent_id, "politica_agente": nueva, "politica_efectiva": despues, "expiradas": expiradas}


@admin_router.put("/{agent_id}/aprobacion-central")
async def cambiar_modo(
    agent_id: str,
    cuerpo: ModoAprobacion,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from sqlalchemy import select

    from hermeshq.models.activity import ActivityLog
    from hermeshq.models.aprobacion import Aprobacion

    agente = await db.get(Agent, agent_id)
    if agente is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    antes = ac.central_activa_para(agente)
    anterior = agente.approval_central_mode
    agente.approval_central_mode = cuerpo.modo
    despues = ac.central_activa_para(agente)
    expiradas: list[str] = []
    if antes and not despues:
        abiertas = (
            (
                await db.execute(
                    select(Aprobacion).where(
                        Aprobacion.agent_id == agent_id,
                        Aprobacion.estado.in_(("pendiente", "aprobada", "ejecutando")),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(a.estado == "ejecutando" for a in abiertas):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "codigo": "ejecucion_en_curso",
                    "error": "Hay un envío en curso o sin conciliar; concílialo antes de desactivar.",
                },
            )
        for aprob in abiertas:
            if await ac._transicion(db, aprob.id, (aprob.estado,), "expirada"):
                ac._evento(db, aprob.id, "config_cambiada", aprob.estado, "expirada", "usuario", admin.id)
                expiradas.append(aprob.id)
    db.add(
        ActivityLog(
            agent_id=agent_id,
            event_type="agent.approval_central_changed",
            message=f"Aprobación central: {anterior} -> {cuerpo.modo}",
            details={"por": admin.id, "efectiva_antes": antes, "efectiva_despues": despues, "expiradas": expiradas},
        )
    )
    await db.commit()
    return {
        "agent_id": agent_id,
        "modo": cuerpo.modo,
        "efectiva": despues,
        "expiradas": expiradas,
        "aviso": "Los plugins toman la configuración al iniciar cada tarea.",
    }
