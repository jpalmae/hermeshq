import re
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from hermeshq.services.marcado_leidos import CLAVE_METADATA, definicion_declara_marcar_leidos

DEFAULT_TIMEZONE = "America/Santiago"
LEGACY_TIMEZONE = "UTC"
MIN_AGENT_INTERVAL_MINUTES = 15
MAX_AGENT_SCHEDULES = 5
MAX_AGENT_PROMPT_CHARS = 2000
MAX_AGENT_NAME_CHARS = 100
_INTERVAL_SAMPLE_SIZE = 60
APPROVAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
APPROVAL_CODE_LENGTH = 6
APPROVAL_CODE_TTL = timedelta(hours=48)


class CronScheduleError(ValueError):
    pass


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or LEGACY_TIMEZONE).strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise CronScheduleError(f"Invalid timezone: {name}") from exc


def _build_iterator(expression: str, base: datetime) -> croniter:
    if len(expression.split()) == 6:
        return croniter(expression, base, second_at_beginning=True)
    return croniter(expression, base)


def compute_next_run(expression: str, timezone_name: str | None, now: datetime | None = None) -> datetime:
    if not croniter.is_valid(expression):
        raise CronScheduleError(f"Invalid cron expression: {expression}")
    zone = resolve_timezone(timezone_name)
    base = (now or datetime.now(UTC)).astimezone(zone)
    iterator = _build_iterator(expression, base)
    for _ in range(4):
        candidate = iterator.get_next(datetime)
        if not _is_repeated_wall_time(candidate, zone):
            return candidate.astimezone(UTC)
    return candidate.astimezone(UTC)


def _is_repeated_wall_time(moment: datetime, zone: ZoneInfo) -> bool:
    local = moment.astimezone(zone)
    wall = local.replace(tzinfo=None)
    first = wall.replace(fold=0).replace(tzinfo=zone)
    second = wall.replace(fold=1).replace(tzinfo=zone)
    if first.utcoffset() == second.utcoffset():
        return False
    return local.astimezone(UTC) == second.astimezone(UTC)


def validate_agent_expression(
    expression: str,
    timezone_name: str | None,
    *,
    min_interval_minutes: int = MIN_AGENT_INTERVAL_MINUTES,
    now: datetime | None = None,
) -> datetime:
    expression = (expression or "").strip()
    if len(expression.split()) != 5:
        raise CronScheduleError("Use a standard 5-field cron expression (minute hour day month weekday)")
    first_run = compute_next_run(expression, timezone_name, now)
    zone = resolve_timezone(timezone_name)
    base = (now or datetime.now(UTC)).astimezone(zone)
    iterator = _build_iterator(expression, base)
    previous = iterator.get_next(datetime).astimezone(UTC)
    for _ in range(_INTERVAL_SAMPLE_SIZE):
        current = iterator.get_next(datetime).astimezone(UTC)
        if (current - previous).total_seconds() < min_interval_minutes * 60:
            raise CronScheduleError(
                f"Schedules created by agents must be at least {min_interval_minutes} minutes apart"
            )
        previous = current
    return first_run


def scheduled_task_metadata(schedule, *, manual: bool = False, workspace_path: str | None = None) -> dict:
    metadata = {
        "scheduled_task_id": schedule.id,
        "scheduled": True,
        "scheduled_definition_version": int(getattr(schedule, "definition_version", None) or 1),
        CLAVE_METADATA: definicion_declara_marcar_leidos(schedule, workspace_path),
    }
    if manual:
        metadata["manual_trigger"] = True
    if schedule.owner_user_id:
        metadata["thread_user_id"] = schedule.owner_user_id
    if schedule.created_by_agent_id:
        metadata["agent_created_schedule"] = True
    return metadata


def generate_approval_code() -> str:
    return "".join(secrets.choice(APPROVAL_CODE_ALPHABET) for _ in range(APPROVAL_CODE_LENGTH))


def codigo_eliminacion(schedule) -> str:
    import hashlib
    import hmac

    from hermeshq.config import get_settings

    clave = (get_settings().jwt_secret or "").encode("utf-8")
    base = f"eliminar:{schedule.id}:{int(getattr(schedule, 'definition_version', None) or 1)}".encode()
    resumen = hmac.new(clave, base, hashlib.sha256).digest()
    return "".join(APPROVAL_CODE_ALPHABET[b % len(APPROVAL_CODE_ALPHABET)] for b in resumen[:APPROVAL_CODE_LENGTH])


def message_contains_code(message: str | None, code: str | None) -> bool:
    if not message or not code:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", message, re.IGNORECASE) is not None


_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def current_time_context(timezone_name: str | None, now: datetime | None = None) -> str:
    zone = resolve_timezone(timezone_name)
    utc_now = (now or datetime.now(UTC)).astimezone(UTC)
    local = utc_now.astimezone(zone)
    raw_offset = local.strftime("%z")
    offset = f"{raw_offset[:3]}:{raw_offset[3:]}"
    return (
        f"[Hora actual: {_WEEKDAYS_ES[local.weekday()]} {local:%Y-%m-%d %H:%M} {zone.key} (UTC{offset}) = "
        f"{utc_now:%Y-%m-%dT%H:%M:%SZ}. No necesitas ejecutar código para saber la hora.]"
    )


CAMPOS_DE_DEFINICION = ("prompt", "output_format", "cron_expression", "timezone")


def cambia_la_definicion(item, cambios: dict) -> bool:

    def _normal(campo, valor):
        texto = " ".join(str(valor).split()) if campo == "cron_expression" else str(valor or "").strip()
        return texto or None

    return any(
        campo in cambios and _normal(campo, cambios[campo]) != _normal(campo, getattr(item, campo, None))
        for campo in CAMPOS_DE_DEFINICION
    )


def bloque_de_formato(schedule) -> str:
    formato = (getattr(schedule, "output_format", None) or "").strip()
    if not formato:
        return ""
    version = int(getattr(schedule, "definition_version", None) or 1)
    return (
        f"[Formato de salida obligatorio de esta tarea programada (versión {version}). Respétalo exactamente en cada "
        "ejecución, también si no hay novedades: mismos títulos, mismo orden de secciones y mismas columnas; no "
        "agregues saludos ni frases como «Listo».]\n"
        f"{formato}"
    )


ENTREGA_AUTOMATICA = (
    "[Entrega: tu respuesta de esta ejecución le llega al usuario automáticamente (por el chat y en HermesHQ). Para "
    "«avisarle», «recordarle» o «enviarle» algo AL USUARIO, solo escríbelo en tu respuesta, en español; no uses "
    "herramientas de correo ni de Teams para eso. Úsalas solo si la tarea pide escribirle a OTRA persona.]"
)


def build_scheduled_prompt(schedule, now: datetime | None = None) -> str:
    base = (schedule.prompt or "").rstrip()
    formato = bloque_de_formato(schedule)
    partes = (
        [base] + ([formato] if formato else []) + [current_time_context(schedule.timezone, now), ENTREGA_AUTOMATICA]
    )
    return "\n\n".join(partes)
