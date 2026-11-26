from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.task import Task

VIGENCIA_PLAN = timedelta(hours=2)
MAX_CORRECCION = 600

_ANALISIS = re.compile(
    r"\b(analiz\w*|determin\w*|detect\w*|identific\w*|cruz\w*|correlacion\w*|clasific\w*|evalu\w*|"
    r"encuentr\w* (los|las) (casos|patrones|reprocesos|duplicados|anomalias)|reprocesos?)\b"
)
_DATOS = re.compile(
    r"\b(excel|xlsx|xls|csv|planillas?|hojas?|datos|archivos? adjuntos?|registros|columnas?)\b|\[archivos adjuntos"
)
_SIN_PLAN = re.compile(
    r"\b(sin (preguntar|plan|confirmar)|no me preguntes|directo|ejecuta(lo)? (directo|ya)|hazlo (directo|ya))\b"
)
_CONFIRMA = re.compile(
    r"^\s*(s[ií]|sip|ok(ey|ay)?|dale|vale|listo|perfecto|de acuerdo|adelante|procede|ejecuta(lo)?|hazlo|confirmo|"
    r"conf[ií]rmalo|est[aá] bien|correcto|go)\b",
    re.IGNORECASE,
)
_CANCELA = re.compile(r"^\s*(no|cancela\w*|olv[ií]dalo|d[eé]jalo|para|det[eé]n\w*)\s*[.!]*\s*$", re.IGNORECASE)

_FUENTE_NOMBRADA = re.compile(
    r"\b(del|de la|de los|de las|sobre el|sobre la|a partir del|a partir de la|basad[oa] en (el|la)|segun el|"
    r"segun la|con el|con la)\s+(procedimientos?|politicas?|documentos?|manual(es)?|instructivos?|contratos?|"
    r"reglamentos?|normas?|estandar(es)?|protocolos?|planes?|informes?|reportes?|presentacion(es)?|archivos?|"
    r"excel|planillas?|actas?|minutas?|propuestas?|cotizacion(es)?)\b"
)
_REFERENCIA_CONVERSACION = re.compile(
    r"\b(que (acabas de|me) (hacer|generar|crear|enviar|mandar|pasar)|que (hiciste|generaste|creaste|preparaste|"
    r"enviaste|mandaste)|que te (mande|envie|pase|adjunte|comparti)|que (adjunte|subi|comparti)|adjunt[oa]s?|"
    r"anterior|de recien|recien hech[oa]|el ultimo|la ultima|de arriba|de este chat|de esta conversacion)\b"
)
_ARCHIVO_EXPLICITO = re.compile(r"\.(pdf|docx?|xlsx?|xlsm|csv|pptx?|txt|md)\b|[a-z]:\\|https?://|onedrive|sharepoint")
_ELECCION = re.compile(
    r"^\s*(el|la|los|las|usa|use|ocupa|toma|ese|esa|este|esta|opci[oó]n|n[uú]mero|#)?\s*"
    r"(primer[oa]?|segund[oa]|tercer[oa]?|[1-3]|ese|esa|este|esta|[uú]ltim[oa]|m[aá]s reciente)\b",
    re.IGNORECASE,
)

DIRECTIVA_FUENTE = (
    "[ETAPA DE PREPARACIÓN: todavía NO generes el documento ni uses archivos que ya estén en tu espacio de otros "
    "temas.\n"
    "1. Ubica la fuente que nombra el pedido: sharepoint_search con las palabras clave del tema (por ejemplo, para "
    "«procedimiento de asfalto»: «procedimiento asfalto»). Revisa también uploads/ y sharepoint_downloads/, pero "
    "SOLO si el nombre del archivo corresponde al tema.\n"
    "2. Responde empezando con «📄 Fuente para tu pedido»: lista 1 a 3 candidatos numerados (nombre, carpeta y "
    "fecha) y pregunta cuál usar. Si no encuentras ninguno, dilo y pide que adjunte el archivo o indique dónde está.\n"
    "3. Termina con: «Responde con el número (o adjunta el archivo) y lo preparo.» No generes nada todavía.]"
)

DIRECTIVA_PLAN = (
    "[ETAPA DE PLAN DEL ANÁLISIS: todavía NO ejecutes el análisis completo ni generes archivos.\n"
    "1. Si el pedido no trae el archivo adjunto, ubícalo primero (sharepoint_search con palabras del pedido) y "
    "nómbralo en el plan; nunca uses archivos de otros temas que haya en tu espacio. Lee la estructura de los datos "
    "con hq_leer_documento (una sola llamada).\n"
    "2. Responde con un plan breve que empiece con «📋 Plan de análisis»:\n"
    "   - Datos que usarás: hojas, cantidad de filas y columnas clave (cifras de la herramienta).\n"
    "   - Criterios concretos y medibles para cada paso: valores numéricos, columnas, cómo decides que dos registros "
    "coinciden, qué excluyes. Nada de «razonable» o «según corresponda».\n"
    "   - Supuestos que tomas sobre los datos.\n"
    "   - Como máximo 3 preguntas, SOLO las que cambian el resultado; cada una con la opción que usarás si el usuario "
    "no responde.\n"
    "3. Termina con: «Responde sí para ejecutar con estos criterios, o dime qué cambiar.»\n"
    "No entregues resultados todavía.]"
)


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texto).lower()


def necesita_plan(contenido: str) -> bool:
    texto = _normalizar(contenido)
    return bool(_ANALISIS.search(texto) and _DATOS.search(texto) and not _SIN_PLAN.search(texto))


def necesita_fuente(contenido: str) -> bool:
    if (contenido or "").lstrip().startswith("[Archivos adjuntos"):
        return False
    texto = _normalizar(contenido)
    return (
        bool(_FUENTE_NOMBRADA.search(texto))
        and not _ARCHIVO_EXPLICITO.search(texto)
        and not _REFERENCIA_CONVERSACION.search(texto)
        and not _SIN_PLAN.search(texto)
    )


def con_directiva_plan(contenido: str, etapa: str = "plan") -> str:
    return f"{DIRECTIVA_FUENTE if etapa == 'fuente' else DIRECTIVA_PLAN}\n\n{contenido}"


def es_cancelacion(mensaje: str) -> bool:
    return bool(_CANCELA.match(mensaje or ""))


_CRITERIO = re.compile(
    r"\d|\b(cambia\w*|usa|usar|considera\w*|excluy\w*|exclu[ií]\w*|inclu\w*|solo|s[oó]lo|sin|agreg\w*|quit\w*|"
    r"ventana|d[ií]as?|semanas?|meses|criterios?|cuent\w*|cuenten|descart\w*|preventiv\w*|correctiv\w*|umbral|"
    r"m[ií]nimo|m[aá]ximo|columna|hoja|equipo|componente|en vez de|en lugar de|mejor)\b",
    re.IGNORECASE,
)


def parece_correccion(mensaje: str) -> bool:
    texto = (mensaje or "").strip()
    return bool(texto) and len(texto) <= MAX_CORRECCION and not texto.endswith("?") and bool(_CRITERIO.search(texto))


def responde_al_plan(mensaje: str) -> bool:
    texto = (mensaje or "").strip()
    if not texto or es_cancelacion(texto):
        return False
    if texto.startswith("[Archivos adjuntos"):
        return True
    return bool(_CONFIRMA.match(texto) or _ELECCION.match(texto)) or parece_correccion(texto)


def pedido_confirmado(pedido_original: str, plan: str, respuesta: str, etapa: str = "plan") -> str:
    resto = _CONFIRMA.sub("", respuesta or "", count=1).strip(" ,.;:-!") if _CONFIRMA.match(respuesta or "") else ""
    correcciones = (respuesta or "").strip() if not _CONFIRMA.match(respuesta or "") or parece_correccion(resto) else ""
    if etapa == "fuente":
        return "\n\n".join(
            p
            for p in (
                pedido_original.strip(),
                "[FUENTE: estos fueron los candidatos que propusiste.]",
                (plan or "").strip(),
                "[ELECCIÓN DEL USUARIO: usa SOLO la fuente que eligió (o el archivo que adjuntó); no uses archivos de "
                "otros temas. Si no puedes abrirla, dilo y termina sin inventar contenido.]",
                (respuesta or "").strip(),
            )
            if p
        )
    partes = [
        pedido_original.strip(),
        "[PLAN CONFIRMADO POR EL USUARIO: aplica EXACTAMENTE estos criterios en todo el análisis; si cambias uno, "
        "dilo en la entrega con el motivo.]",
        (plan or "").strip(),
    ]
    if correcciones:
        partes += [
            "[CORRECCIONES DEL USUARIO AL PLAN: mandan sobre el plan anterior.]",
            correcciones,
        ]
    partes.append(
        "[MIENTRAS TRABAJAS: cuando tengas el primer resultado (totales y primeros casos), envía un avance con "
        "hq_avance y sigue. Si llega una corrección del usuario, aplícala e informa qué casos cambiaron. La entrega "
        "final empieza con los criterios aplicados.]"
    )
    return "\n\n".join(p for p in partes if p)


async def plan_pendiente(db: AsyncSession, agent_id: str, session_id: str) -> Task | None:
    resultado = await db.execute(
        select(Task)
        .where(Task.agent_id == agent_id, Task.queued_at >= datetime.now(UTC) - VIGENCIA_PLAN)
        .order_by(Task.queued_at.desc())
        .limit(30)
    )
    tareas = list(resultado.scalars().all())
    for tarea in tareas:
        metadata = tarea.metadata_json or {}
        if metadata.get("public_session_id") != session_id and metadata.get("origen_public_session_id") != session_id:
            continue
        if metadata.get("plan_task_id"):
            return None
        if metadata.get("plan_analisis"):
            if metadata.get("response_attachments") or "MEDIA:" in (tarea.response or ""):
                return None
            return tarea if tarea.status == "completed" and (tarea.response or "").strip() else None
    return None


def ruta_correcciones(workspace: str | Path, task_id: str) -> Path:
    return Path(workspace) / ".hermes" / f"correcciones-{task_id}.json"


def registrar_correccion(workspace: str | Path, task_id: str, texto: str) -> int:
    ruta = ruta_correcciones(workspace, task_id)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    try:
        actuales = json.loads(ruta.read_text(encoding="utf-8"))
        if not isinstance(actuales, list):
            actuales = []
    except (OSError, ValueError):
        actuales = []
    actuales.append({"texto": texto.strip()[:MAX_CORRECCION], "recibida": datetime.now(UTC).isoformat()})
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(actuales, ensure_ascii=False), encoding="utf-8")
    temporal.replace(ruta)
    return len(actuales)


def aviso_correccion_anotada(texto: str) -> str:
    return (
        f"✍️ Anotado: aplico «{texto.strip()[:160]}» en el análisis que está en curso y te lo confirmo en el próximo "
        "avance o en la entrega."
    )
