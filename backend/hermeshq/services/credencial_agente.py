from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from sqlalchemy import text

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter as _Contador

    _METRICA_ESTATICO = _Contador(
        "hermeshq_agent_static_token_uses_total",
        "Usos del token estático de agente por origen y resultado (para migrar a AGENT_TOKEN_MODE=strict)",
        ["origen", "resultado"],
    )
except Exception:
    _METRICA_ESTATICO = None

_USOS_ESTATICO: dict[tuple[str, str], int] = {}


def _registrar_estatico(origen: str, resultado: str) -> None:
    clave = (origen or "sin_origen", resultado)
    _USOS_ESTATICO[clave] = _USOS_ESTATICO.get(clave, 0) + 1
    if _METRICA_ESTATICO is not None:
        _METRICA_ESTATICO.labels(*clave).inc()


def usos_del_estatico() -> dict[tuple[str, str], int]:
    return dict(_USOS_ESTATICO)


PREFIJO = "v2."
CONTEXTO_CLAVE = b"hq-agent-task-v2"


def _modo() -> str:
    from hermeshq.config import get_settings

    modo = str(getattr(get_settings(), "agent_token_mode", "dual") or "dual").strip().lower()
    return modo if modo in ("off", "dual", "strict") else "dual"


def _clave() -> bytes:
    from hermeshq.config import get_settings

    return hmac.new(get_settings().jwt_secret.encode("utf-8"), CONTEXTO_CLAVE, hashlib.sha256).digest()


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def emision_activa() -> bool:
    return _modo() != "off"


def emitir(agent_id: str, task_id: str, attempt_id: str, generation: int, ttl_segundos: int) -> str:
    carga = {
        "a": str(agent_id),
        "t": str(task_id),
        "x": str(attempt_id),
        "g": int(generation),
        "exp": int(time.time()) + max(60, int(ttl_segundos)),
    }
    cuerpo = _b64(json.dumps(carga, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    firma = _b64(hmac.new(_clave(), cuerpo.encode("ascii"), hashlib.sha256).digest())
    return f"{PREFIJO}{cuerpo}.{firma}"


def decodificar(token: str) -> dict | None:
    if not isinstance(token, str) or not token.startswith(PREFIJO):
        return None
    try:
        cuerpo, firma = token[len(PREFIJO) :].split(".", 1)
        esperada = _b64(hmac.new(_clave(), cuerpo.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(firma, esperada):
            return None
        carga = json.loads(_desb64(cuerpo))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(carga, dict) or int(carga.get("exp") or 0) < time.time():
        return None
    return carga


async def intento_vivo(db, carga: dict) -> bool:
    fila = (
        (
            await db.execute(
                text(
                    "SELECT a.state, a.lease_generation, a.lease_expires_at > now() AS vigente, t.agent_id, t.id AS task_id "
                    "FROM task_execution_attempts a JOIN tasks t ON t.id = a.task_id WHERE a.attempt_id = :x"
                ),
                {"x": carga.get("x")},
            )
        )
        .mappings()
        .first()
    )
    return bool(
        fila
        and fila["state"] == "running"
        and int(fila["lease_generation"]) == int(carga.get("g", -1))
        and fila["vigente"]
        and str(fila["agent_id"]) == str(carga.get("a"))
        and str(fila["task_id"]) == str(carga.get("t"))
    )


async def _token_de_servicio_valido(db, agent_id: str, token: str) -> bool:
    from hermeshq.core.security import decode_device_token_claims, verify_agent_service_token
    from hermeshq.models.agent import Agent
    from hermeshq.models.enrolled_device import EnrolledDevice

    agent = await db.get(Agent, agent_id)
    if agent is None or agent.is_archived:
        return False
    device_claims = decode_device_token_claims(token)
    if device_claims:
        device = await db.get(EnrolledDevice, device_claims.get("did"))
        return bool(
            device
            and device.status == "active"
            and device.agent_id == agent_id
            and device_claims.get("token_version") == device.token_version
        )
    return verify_agent_service_token(token, agent_id, agent.service_token_version or 1)


async def credencial_valida(db, agent_id: str, token: str, *, origen: str = "", cabeceras=None) -> bool:
    if not agent_id or not token:
        return False
    if token.startswith(PREFIJO):
        carga = decodificar(token)
        if carga is None or str(carga.get("a")) != str(agent_id):
            return False
        if cabeceras is not None:
            declarado = str(cabeceras.get("X-HermesHQ-Attempt-ID") or "").strip()
            if declarado and declarado != str(carga.get("x")):
                logger.warning("credencial v2 de %s con un intento declarado distinto (%s)", agent_id, origen)
                return False
        return await intento_vivo(db, carga)
    if not await _token_de_servicio_valido(db, agent_id, token):
        return False
    modo = _modo()
    if modo == "strict":
        _registrar_estatico(origen, "rechazado")
        logger.warning("credencial estática rechazada (modo strict): agente=%s origen=%s", agent_id, origen)
        return False
    _registrar_estatico(origen, "aceptado")
    if modo == "dual":
        logger.info("credencial estática usada: agente=%s origen=%s", agent_id, origen)
    return True
