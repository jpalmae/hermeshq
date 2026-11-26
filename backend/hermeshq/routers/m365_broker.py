from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.task import Task
from hermeshq.services.credencial_agente import credencial_valida
from hermeshq.services.external_actions import (
    ExternalActionDenied,
    begin_external_action,
    fingerprint_for,
    finish_external_action,
    require_live_attempt,
)
from hermeshq.services.graph_broker import (
    AccesoDenegado,
    GraphBroker,
    ProveedorGraphHttp,
    SolicitudBroker,
    politica_desde_configuracion,
    proveedor_simulado_desde_json,
)
from hermeshq.services.m365_oauth import get_valid_token, motivo_sin_token
from hermeshq.services.marcado_leidos import marcado_autorizado
from hermeshq.services.secret_vault import build_vault_from_settings
from hermeshq.services.subida_prohibida import prohibicion_vigente
from hermeshq.services.task_visibility import task_audience_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/control/m365", tags=["m365-broker"], include_in_schema=False)
ESTADOS_ACTIVOS = ("running", "queued")


def ruta_graph_simulado() -> str:
    ruta = os.environ.get("HERMESHQ_GRAPH_SIMULADO_JSON", "")
    if not ruta:
        return ""
    if os.environ.get("HERMESHQ_ENTORNO") != "aislado":
        logger.error("HERMESHQ_GRAPH_SIMULADO_JSON ignorado: solo se admite con HERMESHQ_ENTORNO=aislado")
        return ""
    return ruta


class BrokerRequest(BaseModel):
    tarea_id: str = Field(min_length=1, max_length=64)
    operacion: Literal["list", "read", "search", "download", "upload"]
    sitio_id: str | None = Field(default=None, max_length=256)
    carpeta: str | None = Field(default=None, max_length=1024)
    pagina: int = Field(default=0, ge=0, le=10)
    cursor: str | None = Field(default=None, max_length=128)
    finalidad: str | None = Field(default=None, max_length=64)
    nombre: str | None = Field(default=None, max_length=255)
    contenido_b64: str | None = Field(default=None, max_length=2_000_000)


def _denegar(exc: ExternalActionDenied) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"motivo": exc.motivo, **exc.detalle})


async def _intento_vivo(request: Request, db: AsyncSession, agente: Agent, tarea_id: str) -> None:
    try:
        await require_live_attempt(db, request.headers, agent_id=agente.id, task_id=tarea_id)
    except ExternalActionDenied as exc:
        raise _denegar(exc)


async def _iniciar_accion(
    request: Request, db: AsyncSession, agente: Agent, tarea: Task, usuario_id: str, tipo: str, carga
) -> str:
    agente_id, tarea_id = agente.id, tarea.id
    try:
        return await begin_external_action(
            db, request.headers, agent_id=agente_id, task_id=tarea_id, user_id=usuario_id, kind=tipo, payload=carga
        )
    except ExternalActionDenied as exc:
        await db.rollback()
        logger.warning("m365 acción externa denegada: agente=%s tarea=%s motivo=%s", agente_id, tarea_id, exc.motivo)
        raise _denegar(exc)


async def _agente(request: Request, db: AsyncSession) -> Agent:
    agent_id = request.headers.get("X-HermesHQ-Agent-ID", "").strip()
    token = request.headers.get("X-HermesHQ-Agent-Token", "").strip()
    if not await credencial_valida(db, agent_id, token, origen="m365_broker", cabeceras=request.headers):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent credentials")
    agente = await db.get(Agent, agent_id)
    if not agente or agente.is_archived:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown agent")
    return agente


@router.post("/broker")
async def broker_m365(body: BrokerRequest, request: Request, db: AsyncSession = Depends(get_db_session)) -> dict:
    agente = await _agente(request, db)
    tarea = await db.get(Task, body.tarea_id)
    if not tarea or tarea.agent_id != agente.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La tarea no pertenece a este agente")
    usuario_id = task_audience_user_id(tarea) or ""
    if not usuario_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La tarea no tiene usuario responsable")
    await _intento_vivo(request, db, agente, tarea.id)
    activa = tarea.status in ESTADOS_ACTIVOS
    asignado = (
        await db.execute(
            select(AgentAssignment.id).where(
                AgentAssignment.agent_id == agente.id, AgentAssignment.user_id == usuario_id
            )
        )
    ).scalar_one_or_none() is not None

    politicas = {agente.id: politica_desde_configuracion((agente.integration_configs or {}).get("m365_datos"))}
    auditoria: list[dict] = []
    secreto = get_settings().jwt_secret

    async def fabrica(user_id: str):
        ruta = ruta_graph_simulado()
        if ruta:
            logger.warning("m365_broker: proveedor Graph SIMULADO activo (solo pruebas aisladas)")
            return proveedor_simulado_desde_json(ruta)
        vault = build_vault_from_settings(get_settings())
        token, _, _ = await get_valid_token(user_id, vault, db)
        if not token:
            raise AccesoDenegado(await motivo_sin_token(user_id, db))
        return ProveedorGraphHttp(token)

    contenido = None
    if body.contenido_b64 is not None:
        try:
            contenido = base64.b64decode(body.contenido_b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise HTTPException(status_code=400, detail="contenido_b64 no valido")

    gb = GraphBroker(
        fabrica,
        politicas,
        secreto,
        tarea_activa=lambda u, a, t: activa and u == usuario_id and t == tarea.id,
        asignado=lambda u, a: asignado and u == usuario_id,
        auditoria=auditoria,
    )
    solicitud = SolicitudBroker(
        usuario_id=usuario_id,
        agente_id=agente.id,
        tarea_id=tarea.id,
        operacion=body.operacion,
        sitio_id=body.sitio_id,
        carpeta=body.carpeta,
        pagina=body.pagina,
        cursor=body.cursor,
        finalidad=body.finalidad,
        nombre=body.nombre,
        contenido=contenido,
    )
    accion_id = None
    if body.operacion == "upload":
        accion_id = await _iniciar_accion(
            request,
            db,
            agente,
            tarea,
            usuario_id,
            "broker:upload",
            {"sitio": body.sitio_id, "carpeta": body.carpeta, "nombre": body.nombre, "b64": body.contenido_b64},
        )
    try:
        resultado = await gb.ejecutar(solicitud)
    except AccesoDenegado as exc:
        if accion_id:
            await finish_external_action(db, accion_id, "no_ejecutada", f"denegada:{exc.motivo}")
        logger.warning("m365_broker denegada: agente=%s tarea=%s motivo=%s", agente.id, tarea.id, exc.motivo)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": exc.motivo})
    except Exception:
        logger.exception("m365_broker: resultado incierto de la acción %s", accion_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"motivo": "resultado_incierto"})
    if accion_id:
        await finish_external_action(db, accion_id, "confirmada")
    logger.info("m365_broker permitida: agente=%s tarea=%s operacion=%s", agente.id, tarea.id, body.operacion)
    return resultado


class GraphRequest(BaseModel):
    tarea_id: str = Field(min_length=1, max_length=64)
    metodo: Literal["GET", "POST", "PUT", "PATCH"]
    ruta: str = Field(min_length=1, max_length=2048)
    cuerpo_json: dict | None = None
    cuerpo_b64: str | None = Field(default=None, max_length=36_000_000)
    prefer: str | None = Field(default=None, max_length=80)
    confirmacion_compartir: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


async def _tarea_del_agente(db: AsyncSession, agente: Agent, tarea_id: str) -> tuple[Task, str]:
    tarea = await db.get(Task, tarea_id)
    if not tarea or tarea.agent_id != agente.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La tarea no pertenece a este agente")
    usuario_id = task_audience_user_id(tarea) or ""
    if not usuario_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="La tarea no tiene usuario responsable")
    return tarea, usuario_id


async def _asignado(db: AsyncSession, agente_id: str, usuario_id: str) -> bool:
    fila = await db.execute(
        select(AgentAssignment.id).where(AgentAssignment.agent_id == agente_id, AgentAssignment.user_id == usuario_id)
    )
    return fila.scalar_one_or_none() is not None


@router.get("/contexto")
async def contexto_m365(request: Request, tarea_id: str, db: AsyncSession = Depends(get_db_session)) -> dict:
    agente = await _agente(request, db)
    tarea, usuario_id = await _tarea_del_agente(db, agente, tarea_id)
    await _intento_vivo(request, db, agente, tarea.id)
    if tarea.status not in ESTADOS_ACTIVOS or not await _asignado(db, agente.id, usuario_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin tarea activa o agente no asignado")
    fila = await db.execute(
        select(AgentAssignment.sharepoint_site_url).where(
            AgentAssignment.agent_id == agente.id, AgentAssignment.user_id == usuario_id
        )
    )
    return {"sharepoint_site_url": fila.scalar_one_or_none()}


RUTA_COMPARTIR = re.compile(r"^/me/drive/items/[A-Za-z0-9!_\-]{4,200}/createLink$")
ALCANCES_COMPARTIR = ("organization", "anonymous")
TIPO_VISTA_PREVIA_COMPARTIR = "graph:compartir_vista_previa"
CONFIRMACION_COMPARTIR_TTL_SEGUNDOS = 1800
VISTA_PREVIA_DISPONIBLE = "vista_previa"


def _tarea_automatica(tarea: Task) -> bool:
    md = tarea.metadata_json if isinstance(tarea.metadata_json, dict) else {}
    return bool(
        md.get("scheduled")
        or md.get("scheduled_task_id")
        or md.get("background_job")
        or md.get("source") == "background_job"
    )


def _carga_vista_previa(huella: str, alcance: str) -> dict:
    return {"huella": huella, "alcance": alcance}


class VistaPreviaCompartirRequest(BaseModel):
    tarea_id: str = Field(min_length=1, max_length=64)
    huella: str = Field(pattern=r"^[0-9a-f]{64}$")
    alcance: Literal["organization", "anonymous"]


@router.post("/compartir/vista-previa")
async def registrar_vista_previa_compartir(
    body: VistaPreviaCompartirRequest, request: Request, db: AsyncSession = Depends(get_db_session)
) -> dict:
    from hermeshq.services import aprobacion_central as ac
    from hermeshq.services import graph_proxy as gp

    agente = await _agente(request, db)
    tarea, usuario_id = await _tarea_del_agente(db, agente, body.tarea_id)
    if tarea.status not in ESTADOS_ACTIVOS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "sin_tarea_activa_del_usuario"})
    if not await _asignado(db, agente.id, usuario_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "agente_no_asignado_al_usuario"})
    await _intento_vivo(request, db, agente, tarea.id)
    if ac.central_activa_para(agente):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "accion_externa_requiere_aprobacion"}
        )
    if gp.politica_de_agente(agente.integration_configs)[1] != "compatibilidad":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_no_permitido"})
    if _tarea_automatica(tarea):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_en_tarea_automatica"})
    carga = _carga_vista_previa(body.huella, body.alcance)
    try:
        accion_id = await begin_external_action(
            db,
            request.headers,
            agent_id=agente.id,
            task_id=tarea.id,
            user_id=usuario_id,
            kind=TIPO_VISTA_PREVIA_COMPARTIR,
            payload=carga,
        )
    except ExternalActionDenied as exc:
        await db.rollback()
        raise _denegar(exc)
    await finish_external_action(db, accion_id, "no_ejecutada", VISTA_PREVIA_DISPONIBLE)
    return {"registrada": True}


async def _consumir_vista_previa(db: AsyncSession, agente: Agent, tarea: Task, usuario_id: str, carga: dict):
    fila = (
        await db.execute(
            text(
                "UPDATE external_action_records SET detail = :marca WHERE id = ("
                "SELECT r.id FROM external_action_records r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.kind = :k AND r.fingerprint = :f AND r.state = 'no_ejecutada' AND r.detail = :disponible "
                "AND t.agent_id = :agente AND r.task_id <> :tarea "
                "AND r.created_at > now() - make_interval(secs => :ttl) AND r.created_at < :encolada "
                "ORDER BY r.created_at DESC LIMIT 1 FOR UPDATE OF r SKIP LOCKED"
                ") AND detail = :disponible RETURNING id"
            ),
            {
                "marca": f"consumida:{tarea.id}",
                "disponible": VISTA_PREVIA_DISPONIBLE,
                "k": TIPO_VISTA_PREVIA_COMPARTIR,
                "f": fingerprint_for(usuario_id, TIPO_VISTA_PREVIA_COMPARTIR, carga),
                "agente": agente.id,
                "tarea": tarea.id,
                "ttl": CONFIRMACION_COMPARTIR_TTL_SEGUNDOS,
                "encolada": tarea.queued_at,
            },
        )
    ).first()
    await db.commit()
    return str(fila[0]) if fila else None


async def _devolver_vista_previa(db: AsyncSession, vista_id: str, tarea: Task) -> None:
    await db.execute(
        text("UPDATE external_action_records SET detail = :disponible WHERE id = :id AND detail = :marca"),
        {"id": vista_id, "marca": f"consumida:{tarea.id}", "disponible": VISTA_PREVIA_DISPONIBLE},
    )
    await db.commit()


async def _compartir_sin_aprobacion_central(request, db, agente, tarea, usuario_id, body, origen, token_de) -> dict:
    from hermeshq.services import aprobacion_central as ac
    from hermeshq.services import graph_proxy as gp

    if ac.central_activa_para(agente):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "accion_externa_requiere_aprobacion"}
        )
    if origen != "compatibilidad":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_no_permitido"})
    cuerpo = body.cuerpo_json or {}
    if (
        set(cuerpo) != {"type", "scope"}
        or cuerpo.get("type") != "view"
        or cuerpo.get("scope") not in ALCANCES_COMPARTIR
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_cuerpo_no_permitido"})
    if _tarea_automatica(tarea):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_en_tarea_automatica"})
    if not body.confirmacion_compartir:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_requiere_confirmacion"})
    vista_id = await _consumir_vista_previa(
        db, agente, tarea, usuario_id, _carga_vista_previa(body.confirmacion_compartir, cuerpo["scope"])
    )
    if vista_id is None:
        logger.warning("m365_graph compartir sin confirmación del usuario: agente=%s tarea=%s", agente.id, tarea.id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "compartir_requiere_confirmacion"})
    try:
        accion_id = await _iniciar_accion(
            request, db, agente, tarea, usuario_id, "graph:compartir", {"ruta": body.ruta, "json": cuerpo}
        )
    except HTTPException:
        await _devolver_vista_previa(db, vista_id, tarea)
        raise
    try:
        token = await asyncio.to_thread(token_de)
        estado, datos = await asyncio.to_thread(
            gp._llamar,
            "POST",
            gp.GRAPH_BASE + body.ruta,
            token,
            json.dumps(cuerpo).encode(),
            "application/json",
            1_000_000,
        )
    except Exception:
        logger.exception("m365_graph compartir: resultado incierto de la acción %s", accion_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"motivo": "resultado_incierto"})
    if 200 <= estado < 300:
        await finish_external_action(db, accion_id, "confirmada", f"status:{estado}")
    elif 400 <= estado < 500:
        await finish_external_action(db, accion_id, "no_ejecutada", f"status:{estado}")
        await _devolver_vista_previa(db, vista_id, tarea)
    logger.info("m365_graph compartir: agente=%s tarea=%s status=%s", agente.id, tarea.id, estado)
    try:
        respuesta = json.loads(datos or b"{}")
    except ValueError:
        respuesta = {}
    return {"status": estado, "json": respuesta}


CAMPOS_BUSQUEDA = ("id", "name", "webUrl", "lastModifiedDateTime", "size", "parentReference")
CAMPOS_REFERENCIA = ("driveId", "id", "siteId", "path")


def _cuerpo_de_busqueda_valido(cuerpo: dict | None) -> bool:
    if not isinstance(cuerpo, dict) or set(cuerpo) != {"requests"} or not isinstance(cuerpo["requests"], list):
        return False
    if len(cuerpo["requests"]) != 1 or not isinstance(cuerpo["requests"][0], dict):
        return False
    req = cuerpo["requests"][0]
    consulta = (req.get("query") or {}).get("queryString") if isinstance(req.get("query"), dict) else None
    return (
        set(req) <= {"entityTypes", "query", "from", "size", "fields"}
        and req.get("entityTypes") == ["driveItem"]
        and set(req["query"]) == {"queryString"}
        and isinstance(consulta, str)
        and 0 < len(consulta.strip()) <= 500
        and isinstance(req.get("from", 0), int)
        and 0 <= req.get("from", 0) <= 1000
        and isinstance(req.get("size", 10), int)
        and 1 <= req.get("size", 10) <= 50
        and set(req.get("fields") or []) <= set(CAMPOS_BUSQUEDA)
    )


async def _buscar_archivos_compatibilidad(body, origen, token_de) -> dict:
    from hermeshq.services import graph_proxy as gp

    if origen != "compatibilidad":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "busqueda_global_no_permitida"})
    if not _cuerpo_de_busqueda_valido(body.cuerpo_json):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "busqueda_no_permitida"})
    token = await asyncio.to_thread(token_de)
    estado, datos = await asyncio.to_thread(
        gp._llamar,
        "POST",
        gp.GRAPH_BASE + "/search/query",
        token,
        json.dumps(body.cuerpo_json).encode(),
        "application/json",
        5_000_000,
    )
    try:
        respuesta = json.loads(datos or b"{}")
    except ValueError:
        respuesta = {}
    if not 200 <= estado < 300:
        return {
            "status": estado,
            "json": {"error": (respuesta.get("error") or {}) if isinstance(respuesta, dict) else {}},
        }
    hits = []
    for bloque in respuesta.get("value") or []:
        for contenedor in bloque.get("hitsContainers") or []:
            for hit in contenedor.get("hits") or []:
                recurso = hit.get("resource") or {}
                limpio = {k: recurso[k] for k in CAMPOS_BUSQUEDA if k in recurso and k != "parentReference"}
                referencia = recurso.get("parentReference") or {}
                limpio["parentReference"] = {k: referencia[k] for k in CAMPOS_REFERENCIA if k in referencia}
                hits.append({"resource": limpio})
    return {"status": estado, "json": {"value": [{"hitsContainers": [{"hits": hits}]}]}}


@router.post("/graph")
async def graph_m365(body: GraphRequest, request: Request, db: AsyncSession = Depends(get_db_session)) -> dict:
    from hermeshq.services import graph_proxy as gp

    agente = await _agente(request, db)
    tarea, usuario_id = await _tarea_del_agente(db, agente, body.tarea_id)
    if tarea.status not in ESTADOS_ACTIVOS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "sin_tarea_activa_del_usuario"})
    if not await _asignado(db, agente.id, usuario_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "agente_no_asignado_al_usuario"})
    await _intento_vivo(request, db, agente, tarea.id)
    politica, _origen = gp.politica_de_agente(agente.integration_configs)
    cuerpo_bytes = None
    if body.cuerpo_b64:
        try:
            cuerpo_bytes = base64.b64decode(body.cuerpo_b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise HTTPException(status_code=400, detail="cuerpo_b64 no valido")

    loop = asyncio.get_running_loop()
    vault = build_vault_from_settings(get_settings())

    def token_de() -> str:
        import os as _os

        if _os.environ.get("HERMESHQ_ENTORNO") == "aislado" and _os.environ.get("HERMESHQ_GRAPH_TOKEN_SIMULADO"):
            return _os.environ["HERMESHQ_GRAPH_TOKEN_SIMULADO"]

        async def _obtener() -> str:
            token, _, _ = await get_valid_token(usuario_id, vault, db)
            if not token:
                raise gp.RutaNoPermitida(await motivo_sin_token(usuario_id, db))
            return token

        return asyncio.run_coroutine_threadsafe(_obtener(), loop).result(timeout=60)

    def unidad_propia_de(token: str) -> str | None:
        datos = gp._llamar_real("GET", gp.GRAPH_BASE + "/me/drive?$select=id", token, None, None, 1_000_000)
        return json.loads(datos[1] or b"{}").get("id")

    if body.metodo == "POST" and RUTA_COMPARTIR.match(body.ruta):
        return await _compartir_sin_aprobacion_central(request, db, agente, tarea, usuario_id, body, _origen, token_de)
    if body.metodo == "POST" and body.ruta == "/search/query":
        return await _buscar_archivos_compatibilidad(body, _origen, token_de)

    accion_id = None
    clase = gp.clase_de(body.metodo, body.ruta)
    if gp.es_subida(body.metodo, body.ruta):
        frase = await prohibicion_vigente(db, tarea)
        if frase:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "el_usuario_pidio_no_subir", "frase": frase}
            )
        config_sharepoint = (agente.integration_configs or {}).get("sharepoint")
        sitios_habilitados = isinstance(config_sharepoint, dict) and config_sharepoint.get("subida_a_sitios") is True
        if (
            _origen != "m365_graph"
            and not body.ruta.startswith("/me/drive/")
            and not (sitios_habilitados and body.ruta.startswith("/drives/"))
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "subida_fuera_del_onedrive_propio"}
            )
    if clase == "accion_externa":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "accion_externa_requiere_aprobacion"}
        )
    if clase == "modificacion_reversible" and not marcado_autorizado(tarea.metadata_json, tarea.prompt):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "marcar_leidos_no_declarado_en_tarea_automatica"}
        )
    if clase == "modificacion":
        accion_id = await _iniciar_accion(
            request,
            db,
            agente,
            tarea,
            usuario_id,
            f"graph:{body.metodo} {body.ruta.split('?', 1)[0][:120]}",
            {"ruta": body.ruta, "json": body.cuerpo_json, "b64": body.cuerpo_b64, "prefer": body.prefer},
        )
    try:
        resultado = await asyncio.to_thread(
            gp.ejecutar,
            body.metodo,
            body.ruta,
            body.cuerpo_json,
            cuerpo_bytes,
            politica,
            token_de,
            unidad_propia_de,
            body.prefer,
            {"usuario": usuario_id, "agente": agente.id, "tarea": tarea.id},
            get_settings().jwt_secret,
        )
    except gp.RutaNoPermitida as exc:
        if accion_id:
            await finish_external_action(db, accion_id, "no_ejecutada", f"denegada:{exc.motivo}")
        logger.warning("m365_graph denegada: agente=%s tarea=%s motivo=%s", agente.id, tarea.id, exc.motivo)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": exc.motivo})
    except Exception:
        logger.exception("m365_graph: resultado incierto de la acción %s", accion_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"motivo": "resultado_incierto"})
    if accion_id:
        estado_graph = int(resultado.get("status") or 0) if isinstance(resultado, dict) else 0
        if 200 <= estado_graph < 300:
            await finish_external_action(db, accion_id, "confirmada", f"status:{estado_graph}")
        elif 400 <= estado_graph < 500:
            await finish_external_action(db, accion_id, "no_ejecutada", f"status:{estado_graph}")
    logger.info("m365_graph permitida: agente=%s tarea=%s %s %s", agente.id, tarea.id, body.metodo, body.ruta[:120])
    return resultado


class AdjuntoSubido(BaseModel):
    nombre: str = Field(default="", max_length=255)
    content_id: str = Field(default="", max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=127)
    contenido_b64: str = Field(max_length=34_000_000)


class EnvioCorreoRequest(BaseModel):
    tarea_id: str = Field(min_length=1, max_length=64)
    aprobacion_id: str = Field(min_length=1, max_length=64)
    contenido: dict
    adjuntos: list[AdjuntoSubido] = Field(default_factory=list, max_length=20)


async def token_del_servidor(db: AsyncSession, usuario_id: str) -> str:
    if os.environ.get("HERMESHQ_ENTORNO") == "aislado" and os.environ.get("HERMESHQ_GRAPH_TOKEN_SIMULADO"):
        return os.environ["HERMESHQ_GRAPH_TOKEN_SIMULADO"]
    token, _, _ = await get_valid_token(usuario_id, build_vault_from_settings(get_settings()), db)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": await motivo_sin_token(usuario_id, db)}
        )
    return token


async def _exigir_politica(db: AsyncSession, agente: Agent, aprob) -> None:
    from hermeshq.services import aprobacion_central as ac

    try:
        ac.exigir_operacion_permitida(agente, aprob.tipo_accion, aprob.contenido_json)
    except ac.ErrorAprobacion:
        if aprob.estado in ("pendiente", "aprobada") and await ac._transicion(
            db, aprob.id, (aprob.estado,), "expirada"
        ):
            ac._evento(db, aprob.id, "politica_bloquea", aprob.estado, "expirada", "sistema", None)
            await db.commit()
        raise


def _error_aprobacion(exc) -> HTTPException:
    from hermeshq.routers.aprobaciones import HTTP_POR_CODIGO

    return HTTPException(
        status_code=HTTP_POR_CODIGO.get(exc.codigo, 409), detail={"motivo": exc.codigo, "error": exc.mensaje}
    )


@router.post("/correo/enviar")
async def enviar_correo_aprobado(
    body: EnvioCorreoRequest, request: Request, db: AsyncSession = Depends(get_db_session)
) -> dict:
    from hermeshq.services import aprobacion_central as ac
    from hermeshq.services import correo_central as cc

    agente = await _agente(request, db)
    if not ac.central_activa_para(agente):
        raise HTTPException(status_code=404, detail={"motivo": "aprobacion_central_no_activa"})
    tarea, usuario_id = await _tarea_del_agente(db, agente, body.tarea_id)
    if tarea.status != "running":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "sin_tarea_activa_del_usuario"})
    if not await _asignado(db, agente.id, usuario_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "agente_no_asignado_al_usuario"})
    agente_id, tarea_id = agente.id, tarea.id
    try:
        ref = await require_live_attempt(db, request.headers, agent_id=agente_id, task_id=tarea_id)
    except ExternalActionDenied as exc:
        raise _denegar(exc)
    try:
        ctx = await ac.contexto_desde_tarea(db, agente_id, tarea_id, ref.attempt_id, ref.generation)
        aprob = await ac._cargar(db, body.aprobacion_id)
        if aprob is None or aprob.agent_id != agente_id or aprob.propietario_user_id != ctx.user_id:
            raise ac.ErrorAprobacion("no_encontrada")
        if ac.huella_canonica(aprob.tipo_accion, body.contenido) != aprob.huella:
            raise ac.ErrorAprobacion("huella_distinta")
        await _exigir_politica(db, agente, aprob)
        canon = dict(aprob.contenido_json)
        aprobacion_id, huella = aprob.id, aprob.huella
    except ac.ErrorAprobacion as exc:
        raise _error_aprobacion(exc)
    tipo = aprob.tipo_accion
    subidos = [a.model_dump() for a in body.adjuntos]
    try:
        if tipo == ac.TIPO_CORREO:
            mensaje = cc.construir_mensaje(canon, subidos, aprobacion_id)
        elif tipo == ac.TIPO_RESPONDER:
            adjuntos_ok = cc.adjuntos_verificados(canon, subidos)
        elif subidos:
            raise cc.EnvioRechazado("adjunto_no_aprobado")
    except cc.EnvioRechazado as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"motivo": exc.motivo})
    token = await token_del_servidor(db, usuario_id)
    if tipo in (ac.TIPO_RESPONDER, ac.TIPO_REENVIAR):
        from hermeshq.services import graph_proxy as gpx

        politica, _origen = gpx.politica_de_agente(agente.integration_configs)
        try:
            await asyncio.to_thread(
                gpx._exigir_mensaje_autorizado, canon["mensaje_origen_id"], politica.correo, lambda: token
            )
        except gpx.RutaNoPermitida as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": exc.motivo})
    try:
        accion_id = await begin_external_action(
            db,
            request.headers,
            agent_id=agente_id,
            task_id=tarea_id,
            user_id=usuario_id,
            kind=tipo,
            payload={"aprobacion": aprobacion_id, "huella": huella},
        )
    except ExternalActionDenied as exc:
        await db.rollback()
        logger.warning("correo central denegado: agente=%s tarea=%s motivo=%s", agente_id, tarea_id, exc.motivo)
        raise _denegar(exc)
    try:
        reclamada = await ac.reclamar_ejecucion(db, aprobacion_id, ctx, body.contenido)
    except ac.ErrorAprobacion as exc:
        await db.rollback()
        await finish_external_action(db, accion_id, "no_ejecutada", f"aprobacion:{exc.codigo}")
        raise _error_aprobacion(exc)
    clave = reclamada.idempotency_key
    por_borrador = tipo != ac.TIPO_CORREO or cc.requiere_borrador(mensaje)
    if por_borrador:
        try:
            if tipo == ac.TIPO_CORREO:
                borrador_id = await asyncio.to_thread(cc.preparar_borrador_nuevo, token, mensaje)
            else:
                borrador_id = await asyncio.to_thread(
                    cc.preparar_respuesta_o_reenvio,
                    token,
                    tipo,
                    canon,
                    adjuntos_ok if tipo == ac.TIPO_RESPONDER else [],
                    aprobacion_id,
                )
        except Exception as exc:  # noqa: BLE001
            motivo = exc.motivo if isinstance(exc, cc.EnvioRechazado) else type(exc).__name__
            await ac.registrar_resultado(db, aprobacion_id, ctx, clave, False, {"paso": "borrador", "motivo": motivo})
            await finish_external_action(db, accion_id, "no_ejecutada", f"borrador:{motivo}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"motivo": "graph_rechazo", "paso": "borrador", "error": motivo, "envio_estado": "fallida"},
            )
    try:
        if not por_borrador:
            estado_graph, _ = await asyncio.to_thread(cc.enviar, token, mensaje)
        else:
            estado_graph, _ = await asyncio.to_thread(cc.enviar_borrador, token, borrador_id)
    except Exception as exc:  # noqa: BLE001
        await ac.registrar_resultado(
            db, aprobacion_id, ctx, clave, False, {"incierto": True, "error": type(exc).__name__}
        )
        logger.warning("correo central incierto: aprobacion=%s error=%s", aprobacion_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "resultado_incierto",
                "envio_estado": "desconocida",
                "aprobacion_id": aprobacion_id,
                "error": ac.FRASE_ENVIO["desconocida"],
            },
        )
    if 200 <= estado_graph < 300:
        final = await ac.registrar_resultado(
            db, aprobacion_id, ctx, clave, True, {"status": estado_graph, "accion_externa": accion_id}
        )
        await finish_external_action(db, accion_id, "confirmada", f"aceptada_por_graph:{estado_graph}")
        return {
            "ok": True,
            "aprobacion_id": aprobacion_id,
            "envio_estado": final.envio_estado,
            "entregado": False,
            "mensaje": ac.FRASE_ENVIO[final.envio_estado],
        }
    if 400 <= estado_graph < 500:
        await ac.registrar_resultado(db, aprobacion_id, ctx, clave, False, {"status": estado_graph})
        await finish_external_action(db, accion_id, "no_ejecutada", f"status:{estado_graph}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"motivo": "graph_rechazo", "status": estado_graph, "envio_estado": "fallida"},
        )
    await ac.registrar_resultado(db, aprobacion_id, ctx, clave, False, {"incierto": True, "status": estado_graph})
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "motivo": "resultado_incierto",
            "status": estado_graph,
            "envio_estado": "desconocida",
            "aprobacion_id": aprobacion_id,
            "error": ac.FRASE_ENVIO["desconocida"],
        },
    )


def _ruta_directa_equivalente(token: str, aprobada: str, recibida: str) -> str:
    from hermeshq.services import graph_central as gc

    try:
        persona = gc.destinatario_directo(aprobada)
        if persona is None or recibida == aprobada:
            return recibida
        chat = gc.buscar_chat_directo(token, persona)
    except gc.AccionRechazada:
        return recibida
    return aprobada if chat and recibida == f"/me/chats/{chat}/messages" else recibida


def _motivo_de_fallo(resultado: dict | None) -> str:
    datos = resultado or {}
    if datos.get("motivo"):
        return f"No se ejecutó ({datos.get('paso') or 'ejecución'}: {datos['motivo']})."
    if datos.get("status"):
        return f"Microsoft 365 respondió {datos['status']}" + (f": {datos['error']}" if datos.get("error") else ".")
    return "Microsoft 365 rechazó la acción."


class AccionGraphRequest(BaseModel):
    tarea_id: str = Field(min_length=1, max_length=64)
    aprobacion_id: str = Field(min_length=1, max_length=64)
    contenido: dict
    archivo_b64: str | None = Field(default=None, max_length=6_000_000)


@router.post("/accion/ejecutar")
async def ejecutar_accion_aprobada(
    body: AccionGraphRequest, request: Request, db: AsyncSession = Depends(get_db_session)
) -> dict:
    from hermeshq.services import aprobacion_central as ac
    from hermeshq.services import graph_central as gc

    agente = await _agente(request, db)
    if not ac.central_activa_para(agente):
        raise HTTPException(status_code=404, detail={"motivo": "aprobacion_central_no_activa"})
    tarea, usuario_id = await _tarea_del_agente(db, agente, body.tarea_id)
    if tarea.status != "running":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "sin_tarea_activa_del_usuario"})
    if not await _asignado(db, agente.id, usuario_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"motivo": "agente_no_asignado_al_usuario"})
    agente_id, tarea_id = agente.id, tarea.id
    try:
        ref = await require_live_attempt(db, request.headers, agent_id=agente_id, task_id=tarea_id)
    except ExternalActionDenied as exc:
        raise _denegar(exc)
    token = await token_del_servidor(db, usuario_id)
    contenido = dict(body.contenido)
    try:
        ctx = await ac.contexto_desde_tarea(db, agente_id, tarea_id, ref.attempt_id, ref.generation)
        aprob = await ac._cargar(db, body.aprobacion_id)
        if aprob is None or aprob.agent_id != agente_id or aprob.propietario_user_id != ctx.user_id:
            raise ac.ErrorAprobacion("no_encontrada")
        if aprob.tipo_accion not in ac.TIPOS_GRAPH:
            raise ac.ErrorAprobacion("tipo_desconocido")
        if aprob.tipo_accion == ac.TIPO_TEAMS:
            contenido["ruta"] = await asyncio.to_thread(
                _ruta_directa_equivalente,
                token,
                str((aprob.contenido_json or {}).get("ruta") or ""),
                str(contenido.get("ruta") or ""),
            )
            try:
                contenido["miembros"] = await asyncio.to_thread(
                    gc.miembros_del_chat, token, str(contenido.get("ruta") or "")
                )
            except gc.AccionRechazada as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"motivo": exc.motivo})
        if aprob.tipo_accion == ac.TIPO_ONEDRIVE:
            try:
                contenido["archivo"] = await asyncio.to_thread(
                    gc.archivo_de_item, token, str(contenido.get("ruta") or "")
                )
            except gc.AccionRechazada as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"motivo": exc.motivo})
        if ac.huella_canonica(aprob.tipo_accion, contenido) != aprob.huella:
            raise ac.ErrorAprobacion("huella_distinta")
        await _exigir_politica(db, agente, aprob)
        canon, aprobacion_id, huella, tipo = dict(aprob.contenido_json), aprob.id, aprob.huella, aprob.tipo_accion
        incierta = aprob.estado == "ejecutando" and aprob.envio_estado == "desconocida"
        ya_ejecutada = aprob.estado == "ejecutada"
        estado_fallido = aprob.estado == "fallida"
        motivo_fallo = _motivo_de_fallo(aprob.resultado_json)
        resultado_previo = dict((aprob.resultado_json or {}).get("respuesta") or {})
        if isinstance(resultado_previo.get("enlace"), str):
            resultado_previo.setdefault("link", {"webUrl": resultado_previo["enlace"]})
    except ac.ErrorAprobacion as exc:
        raise _error_aprobacion(exc)
    if estado_fallido:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"motivo": "fallida", "error": motivo_fallo})
    if ya_ejecutada:
        logger.info("accion graph repetida tras ejecutarse en servidor: aprobacion=%s", aprobacion_id)
        return {
            "ok": True,
            "ya_ejecutada": True,
            "aprobacion_id": aprobacion_id,
            "envio_estado": aprob.envio_estado,
            "resultado": resultado_previo,
        }
    archivo = None
    try:
        if tipo == ac.TIPO_TEAMS and canon.get("archivo"):
            archivo = base64.b64decode((body.archivo_b64 or "").encode("ascii"), validate=True)
            if (
                len(archivo) != canon["archivo"]["bytes"]
                or hashlib.sha256(archivo).hexdigest() != canon["archivo"]["sha256"]
            ):
                raise ValueError("adjunto_distinto_de_lo_aprobado")
        elif body.archivo_b64:
            raise ValueError("adjunto_no_aprobado")
    except (ValueError, binascii.Error) as exc:
        motivo = (
            str(exc) if str(exc) in ("adjunto_distinto_de_lo_aprobado", "adjunto_no_aprobado") else "adjunto_no_valido"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"motivo": motivo})
    if incierta:
        from hermeshq.services import conciliacion_graph as cg

        try:
            final, resultado, dato = await cg.conciliar_automatico(db, aprobacion_id, token)
        except ac.ErrorAprobacion as exc:
            logger.warning("conciliación graph no resuelta: aprobacion=%s motivo=%s", aprobacion_id, exc.codigo)
            raise _error_aprobacion(exc)
        if final.envio_estado == "confirmada":
            logger.info("conciliación graph: ya aplicada, no se repite: aprobacion=%s", aprobacion_id)
            return {
                "ok": True,
                "aprobacion_id": aprobacion_id,
                "conciliada": True,
                "ya_aplicada": True,
                "resultado": {"id": dato},
            }
        logger.info("conciliación graph: no aplicada, se ejecuta una vez: aprobacion=%s", aprobacion_id)
    try:
        accion_id = await begin_external_action(
            db,
            request.headers,
            agent_id=agente_id,
            task_id=tarea_id,
            user_id=usuario_id,
            kind=tipo,
            payload={"aprobacion": aprobacion_id, "huella": huella},
        )
    except ExternalActionDenied as exc:
        await db.rollback()
        raise _denegar(exc)
    try:
        reclamada = await ac.reclamar_ejecucion(db, aprobacion_id, ctx, contenido)
    except ac.ErrorAprobacion as exc:
        await db.rollback()
        await finish_external_action(db, accion_id, "no_ejecutada", f"aprobacion:{exc.codigo}")
        raise _error_aprobacion(exc)
    clave = reclamada.idempotency_key
    try:
        from hermeshq.services import conciliacion_graph as cg

        estado_graph, respuesta = await asyncio.to_thread(
            gc.ejecutar, token, cg.preparar_ejecucion(canon, aprobacion_id), aprobacion_id, archivo
        )
    except gc.AccionRechazada as exc:
        await ac.registrar_resultado(
            db, aprobacion_id, ctx, clave, False, {"paso": "preparacion", "motivo": exc.motivo}
        )
        await finish_external_action(db, accion_id, "no_ejecutada", f"preparacion:{exc.motivo}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"motivo": "graph_rechazo", "paso": "preparacion", "error": exc.motivo},
        )
    except Exception as exc:  # noqa: BLE001
        await ac.registrar_resultado(
            db, aprobacion_id, ctx, clave, False, {"incierto": True, "error": type(exc).__name__}
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "resultado_incierto",
                "aprobacion_id": aprobacion_id,
                "error": "No sé si la operación se aplicó; hay que conciliarla antes de repetir.",
            },
        )
    if 200 <= estado_graph < 300:
        proveedor = str(respuesta.get("id") or "") if isinstance(respuesta, dict) else ""
        await ac.registrar_resultado(
            db,
            aprobacion_id,
            ctx,
            clave,
            True,
            {"status": estado_graph, "proveedor_id": proveedor or None, "confirmada_por_proveedor": bool(proveedor)},
        )
        await finish_external_action(db, accion_id, "confirmada", f"status:{estado_graph}")
        return {"ok": True, "aprobacion_id": aprobacion_id, "status": estado_graph, "resultado": respuesta}
    if 400 <= estado_graph < 500:
        await ac.registrar_resultado(db, aprobacion_id, ctx, clave, False, {"status": estado_graph})
        await finish_external_action(db, accion_id, "no_ejecutada", f"status:{estado_graph}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "graph_rechazo",
                "status": estado_graph,
                "error": (respuesta.get("error") or {}).get("message") if isinstance(respuesta, dict) else None,
            },
        )
    await ac.registrar_resultado(db, aprobacion_id, ctx, clave, False, {"incierto": True, "status": estado_graph})
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"motivo": "resultado_incierto", "status": estado_graph, "aprobacion_id": aprobacion_id},
    )
