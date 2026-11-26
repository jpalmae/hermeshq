from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

OPERACIONES_LECTURA = frozenset({"list", "read", "search", "download"})
OPERACIONES_ESCRITURA = frozenset({"upload"})
FINALIDADES_ESCRITURA = frozenset({"entregable_solicitado"})
MAX_ELEMENTOS_POR_PAGINA = 20
MAX_PAGINAS = 5
_SEGMENTO_PROHIBIDO = re.compile(r"(^|/)(\.\.|\.)(/|$)", re.I)
_CODIFICADO_SOSPECHOSO = re.compile(r"%2e|%2f|%5c|\\", re.I)


@dataclass(frozen=True)
class PoliticaDatos:
    operaciones: frozenset[str] = frozenset()
    carpetas_lectura: tuple[str, ...] = ()
    carpetas_escritura: tuple[str, ...] = ()


@dataclass(frozen=True)
class Solicitud:
    usuario_id: str
    agente_id: str
    tarea_id: str
    operacion: str
    sitio_id: str | None = None
    carpeta: str | None = None
    finalidad: str | None = None
    pagina: int = 0
    cursor: str | None = None
    tarea_activa: bool = False
    asignado: bool = False


@dataclass(frozen=True)
class Decision:
    permitido: bool
    motivo: str
    registro: dict = field(default_factory=dict)


def _normalizar_carpeta(carpeta: str | None) -> str | None:
    if carpeta is None:
        return None
    if _CODIFICADO_SOSPECHOSO.search(carpeta) or _SEGMENTO_PROHIBIDO.search(carpeta):
        return "__invalida__"
    limpia = carpeta.strip().strip("/")
    return limpia


def _dentro(carpeta: str, permitidas: tuple[str, ...]) -> bool:
    return any(carpeta == base or carpeta.startswith(base + "/") for base in permitidas)


def cursor_para(secreto: str, usuario_id: str, agente_id: str, carpeta: str, pagina: int, tarea_id: str) -> str:
    cuerpo = f"{usuario_id}|{agente_id}|{tarea_id}|{carpeta}|{pagina}"
    firma = hmac.new(secreto.encode(), cuerpo.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{pagina}.{firma}"


def _cursor_valido(secreto: str, solicitud: Solicitud, carpeta: str) -> bool:
    if solicitud.cursor is None:
        return solicitud.pagina == 0
    esperado = cursor_para(
        secreto, solicitud.usuario_id, solicitud.agente_id, carpeta, solicitud.pagina, solicitud.tarea_id
    )
    return hmac.compare_digest(solicitud.cursor, esperado) and 0 <= solicitud.pagina < MAX_PAGINAS


def autorizar(
    solicitud: Solicitud, politica: PoliticaDatos, secreto_cursor: str, ahora: datetime | None = None
) -> Decision:
    ahora = ahora or datetime.now(UTC)
    base = {
        "ts": ahora.isoformat(),
        "usuario": solicitud.usuario_id,
        "agente": solicitud.agente_id,
        "tarea": solicitud.tarea_id,
        "operacion": solicitud.operacion,
        "sitio": solicitud.sitio_id,
        "carpeta": solicitud.carpeta,
        "finalidad": solicitud.finalidad,
    }

    def no(motivo: str) -> Decision:
        return Decision(False, motivo, {**base, "decision": "denegada", "motivo": motivo})

    if not solicitud.asignado:
        return no("agente_no_asignado_al_usuario")
    if not solicitud.tarea_activa:
        return no("sin_tarea_activa_del_usuario")
    if solicitud.operacion not in politica.operaciones:
        return no("operacion_no_permitida_para_el_agente")
    if not solicitud.sitio_id:
        return no("listado_o_busqueda_global_sin_sitio")

    carpeta = _normalizar_carpeta(solicitud.carpeta or "")
    if carpeta == "__invalida__":
        return no("ruta_con_segmentos_prohibidos")

    if solicitud.operacion in OPERACIONES_ESCRITURA:
        if solicitud.finalidad not in FINALIDADES_ESCRITURA:
            return no("escritura_sin_finalidad_declarada")
        if not carpeta or not _dentro(carpeta, politica.carpetas_escritura):
            return no("escritura_fuera_de_carpeta_de_salida")
    else:
        if not carpeta:
            return no("lectura_sin_carpeta_explicita")
        if not _dentro(carpeta, politica.carpetas_lectura):
            return no("carpeta_fuera_del_alcance_permitido")
        if solicitud.pagina >= MAX_PAGINAS:
            return no("limite_de_paginacion_superado")
        if not _cursor_valido(secreto_cursor, solicitud, carpeta):
            return no("cursor_no_valido_para_esta_solicitud")

    return Decision(
        True,
        "permitida",
        {**base, "decision": "permitida", "carpeta": carpeta, "max_elementos": MAX_ELEMENTOS_POR_PAGINA},
    )
