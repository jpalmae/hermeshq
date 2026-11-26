from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path

from sqlalchemy import select

from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.task import Task
from hermeshq.services import referencias_correo

logger = logging.getLogger(__name__)

MAX_ARCHIVOS = 10
MAX_TAREAS_CANDIDATAS = 200
CLAVE_SESION_ORIGEN = "origen_public_session_id"
ENCABEZADO = "[Archivos generados en esta conversación (más reciente primero):"
AVISO_CONVERSACION_NUEVA = (
    "[Esta conversación empezó de nuevo (pasó más de una hora desde el último mensaje) y no tienes los turnos "
    "anteriores. Si el usuario se refiere a «ese/esa…», «el que me enviaste» o «el anterior», no sabes cuál es: no lo "
    "busques ni lo elijas por su nombre; pregúntale cuál es (nombre o título) antes de modificar nada.]"
)
AVISO_CONVERSACION_REINICIADA = (
    "[El usuario reinició esta conversación (/reset): no tienes los turnos anteriores. Si se refiere a «ese/esa…», "
    "«el que me enviaste» o «el anterior», no sabes cuál es: no lo busques ni lo elijas por su nombre; pregúntale cuál "
    "es (nombre o título) antes de modificar nada.]"
)
_ALUDE_A_ALGO_ANTERIOR = re.compile(
    r"\b(?:es[eao]s?|aquel(?:la|los|las)?|mism[oa]s?|anterior(?:es)?|"
    r"(?:que|el que|la que|lo que) (?:me )?(?:enviaste|mandaste|hiciste|generaste|creaste|armaste|acabas de))\b"
)
ENCABEZADO_NOMBRADOS = "[Archivos de tu espacio de trabajo que coinciden con lo que nombra el usuario:"
CARPETAS_DE_ARCHIVOS = ("outputs", "output", "uploads", "work")
MAX_NOMBRADOS = 5
MAX_ENTRADAS_RECORRIDO = 4000
_EXTENSIONES = "pptx|docx|xlsx|pdf|csv|html"
_NOMBRE_ENTRE_COMILLAS = re.compile(rf"[«\"“`']([^«»\"“”`'\n]{{1,120}}\.(?:{_EXTENSIONES}))[»\"”`']", re.I)
_NOMBRE_SUELTO = re.compile(rf"(?<![\w/\\.-])([\w][\w.-]{{0,118}}\.(?:{_EXTENSIONES}))(?![\w])", re.I)
_BASE_ENTRE_COMILLAS = re.compile(r"[«\"“`']([^«»\"“”`'\n]{1,120})[»\"”`']")
_BASE_CON_SEPARADOR = re.compile(r"(?<![\w/\\.-])(\w+(?:[-_]\w+)+)(?![\w/\\-])")
_PEDIDO_DEL_USUARIO = "Pedido del usuario:"
_LINEA_MEDIA = re.compile(r"^\s*MEDIA:\s*(\S+)\s*$", re.MULTILINE)


def _workspace(agent) -> Path | None:
    ruta = str(getattr(agent, "workspace_path", "") or "").strip()
    if not ruta:
        return None
    path = Path(ruta)
    if path.is_absolute():
        return path
    return (Path(__file__).resolve().parents[2] / path).resolve()


def _archivo_en_workspace(workspace: Path, relativa: str) -> bool:
    try:
        raiz = workspace.resolve()
        destino = (raiz / relativa).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if raiz not in destino.parents:
        return False
    return destino.is_file()


def _sesion_de(md: dict) -> str:
    return str(md.get("public_session_id") or md.get(CLAVE_SESION_ORIGEN) or "")


def _nombres(md: dict) -> dict:
    nombres = {}
    for adjunto in md.get("response_attachments") or []:
        if isinstance(adjunto, dict) and adjunto.get("file_id") and adjunto.get("filename"):
            nombres[str(adjunto["file_id"])] = str(adjunto["filename"])
    return nombres


def alude_a_algo_anterior(prompt: str) -> bool:
    texto = re.sub(r"\[[^\]]*\]", " ", str(prompt or ""))
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    return bool(_ALUDE_A_ALGO_ANTERIOR.search(plano))


async def sesion_sin_historial(db, agent_id: str, session_id: str) -> bool:
    if not agent_id or not session_id:
        return False
    resultado = await db.execute(
        select(Task.id)
        .where(Task.agent_id == agent_id, Task.metadata_json["public_session_id"].as_string() == session_id)
        .limit(1)
    )
    return resultado.first() is None


async def sesion_recien_reiniciada(db, agent_id: str, session_id: str) -> bool:
    if not agent_id or not session_id:
        return False
    resultado = await db.execute(
        select(Task.metadata_json)
        .where(Task.agent_id == agent_id, Task.metadata_json["public_session_id"].as_string() == session_id)
        .order_by(Task.queued_at.desc())
        .limit(1)
    )
    fila = resultado.first()
    metadata = (fila[0] if fila else None) or {}
    return bool(metadata.get("reset") or metadata.get("auto_reset"))


async def archivos_generados(db, agent_id: str, thread_user_id: str, session_id: str, api_key_id) -> list[dict]:
    if not agent_id or not thread_user_id or not session_id or thread_user_id.startswith("__"):
        return []
    agente = await db.get(Agent, agent_id)
    workspace = _workspace(agente)
    if workspace is None:
        return []
    clave = str(api_key_id or "")
    resultado = await db.execute(
        select(Task)
        .where(Task.agent_id == agent_id, Task.queued_at >= utcnow() - referencias_correo.VENTANA_IMPLICITA)
        .order_by(Task.queued_at.desc())
        .limit(MAX_TAREAS_CANDIDATAS)
    )
    archivos: list[dict] = []
    vistos: set[str] = set()
    for previa in resultado.scalars().all():
        md = previa.metadata_json or {}
        if str(md.get("thread_user_id") or "") != thread_user_id:
            continue
        misma_clave = str(md.get("public_chat_api_key_id") or "") == clave
        if (md.get("reset") or md.get("auto_reset")) and (md.get("public_session_id") == session_id or misma_clave):
            break
        if md.get("scheduled") or not misma_clave or _sesion_de(md) != session_id:
            continue
        if previa.status != "completed" or not previa.response:
            continue
        nombres = _nombres(md)
        for ruta in _LINEA_MEDIA.findall(previa.response):
            ruta = ruta.strip().strip("`'\"")
            if ruta in vistos or not _archivo_en_workspace(workspace, ruta):
                continue
            vistos.add(ruta)
            archivos.append({"ruta": ruta, "task_id": str(previa.id), "nombre": nombres.get(Path(ruta).stem)})
            if len(archivos) >= MAX_ARCHIVOS:
                return archivos
    return archivos


def nombres_citados(prompt: str) -> list[str]:
    texto = str(prompt or "")
    if _PEDIDO_DEL_USUARIO in texto:
        texto = texto.rsplit(_PEDIDO_DEL_USUARIO, 1)[1]
    encontrados: list[str] = []
    entre_comillas = _NOMBRE_ENTRE_COMILLAS.findall(texto)
    resto = _NOMBRE_ENTRE_COMILLAS.sub(" ", texto)
    for nombre in [*entre_comillas, *_NOMBRE_SUELTO.findall(resto)]:
        base = Path(nombre.strip()).name.lower()
        if base and base not in encontrados:
            encontrados.append(base)
    return encontrados[:MAX_NOMBRADOS]


def bases_citadas(prompt: str) -> list[str]:
    texto = str(prompt or "")
    if _PEDIDO_DEL_USUARIO in texto:
        texto = texto.rsplit(_PEDIDO_DEL_USUARIO, 1)[1]
    encontradas: list[str] = []
    for base in [*_BASE_ENTRE_COMILLAS.findall(texto), *_BASE_CON_SEPARADOR.findall(texto)]:
        base = base.strip().lower()
        if len(base) >= 3 and base not in encontradas:
            encontradas.append(base)
    return encontradas[: MAX_NOMBRADOS * 4]


_UBICADO_EN_LA_NUBE = re.compile(
    r"(?:est[aá]|se encuentra|guardad[oa]|ubicad[oa]|alojad[oa])\s+(?:ya\s+)?(?:en|dentro de)\s+(?:mi\s+|el\s+|la\s+|tu\s+)?"
    r"(?:onedrive|sharepoint|carpeta|unidad|nube)|\b(?:de|desde)\s+(?:mi\s+|el\s+|la\s+)?(?:onedrive|sharepoint)\b|"
    rf"(?:onedrive|sharepoint)[^\n\"“”«»]{{0,300}}[\\/][^\\/\n\"“”«»]+\.(?:{_EXTENSIONES})\b",
    re.IGNORECASE,
)


def _dice_que_esta_en_la_nube(prompt: str) -> bool:
    texto = str(prompt or "")
    if _PEDIDO_DEL_USUARIO in texto:
        texto = texto.rsplit(_PEDIDO_DEL_USUARIO, 1)[1]
    return bool(_UBICADO_EN_LA_NUBE.search(texto))


def _clave_de_nombre(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[\s_-]+", "-", plano).strip("-")


_ADJUNTO_CON_NOMBRE = re.compile(r"^\s*-\s+(.+?)\s+\([^)]*\)\s+->\s+\S+", re.MULTILINE)


def _nombres_adjuntos(prompt: str) -> set[str]:
    return {m.group(1).strip().lower() for m in _ADJUNTO_CON_NOMBRE.finditer(str(prompt or ""))}


def archivos_nombrados(workspace: Path | None, prompt: str) -> list[str]:
    if _dice_que_esta_en_la_nube(prompt):
        return []
    nombres = nombres_citados(prompt)
    bases = bases_citadas(prompt)
    if workspace is None or not (nombres or bases):
        return []
    try:
        raiz = workspace.resolve()
    except (OSError, RuntimeError):
        return []
    hallados: list[str] = []
    entradas = 0
    claves_de_bases = {_clave_de_nombre(b) for b in bases}
    for carpeta in CARPETAS_DE_ARCHIVOS:
        base = raiz / carpeta
        if not base.is_dir():
            continue
        for actual, _dirs, archivos in os.walk(base):
            for archivo in sorted(archivos):
                entradas += 1
                if entradas > MAX_ENTRADAS_RECORRIDO:
                    break
                minuscula = archivo.lower()
                coincide_base = (
                    Path(minuscula).suffix.lstrip(".") in _EXTENSIONES.split("|")
                    and _clave_de_nombre(Path(minuscula).stem) in claves_de_bases
                )
                if minuscula in nombres or coincide_base:
                    relativa = (Path(actual) / archivo).relative_to(raiz).as_posix()
                    if relativa not in hallados:
                        hallados.append(relativa)
            if entradas > MAX_ENTRADAS_RECORRIDO:
                break
    adjuntos = _nombres_adjuntos(prompt)
    if adjuntos:
        hallados = [h for h in hallados if Path(h).name.lower() not in adjuntos]
    return hallados[: MAX_NOMBRADOS * 2]


def linea_nombrados(rutas: list[str]) -> str | None:
    if not rutas:
        return None
    return (
        f"{ENCABEZADO_NOMBRADOS} {', '.join(rutas)}. Si el pedido se refiere a ese archivo, ya está en tu espacio de "
        "trabajo: úsalo desde esa ruta; no lo busques en SharePoint ni digas que no existe sin revisar ahí.]"
    )


def linea_archivos(archivos: list[dict]) -> str | None:
    if not archivos:
        return None
    partes = []
    for archivo in archivos:
        detalle = f"tarea {archivo['task_id'][:8]}"
        if archivo.get("nombre"):
            detalle = f"«{archivo['nombre']}», {detalle}"
        partes.append(f"{archivo['ruta']} ({detalle})")
    return (
        f"{ENCABEZADO} {', '.join(partes)}. Si el pedido se refiere a un archivo que ya generaste en esta "
        "conversación («el Excel que acabas de hacer»), usa ese archivo de tu workspace; no lo busques en "
        "SharePoint ni lo vuelvas a adjuntar salvo que te lo pidan. Para leer su contenido (Word, Excel, PowerPoint, "
        "PDF o HTML) usa hq_leer_documento con esa ruta: devuelve el texto limpio; read_file sobre un HTML largo se "
        "corta y repetirlo no avanza.]"
    )


async def con_archivos_generados(
    db, agent_id: str, thread_user_id: str, session_id: str, api_key_id, prompt: str, aviso_conversacion_nueva=False
) -> str:
    if any(
        marca in (prompt or "")
        for marca in (ENCABEZADO, ENCABEZADO_NOMBRADOS, AVISO_CONVERSACION_NUEVA, AVISO_CONVERSACION_REINICIADA)
    ):
        return prompt
    nota_nombrados = None
    try:
        agente = await db.get(Agent, agent_id) if agent_id else None
        nota_nombrados = linea_nombrados(archivos_nombrados(_workspace(agente) if agente else None, prompt))
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron resolver los archivos nombrados en el pedido (agente %s)", agent_id)
    try:
        archivos = await archivos_generados(db, agent_id, thread_user_id, session_id, api_key_id)
        if not archivos and not nota_nombrados and aviso_conversacion_nueva and alude_a_algo_anterior(prompt):
            if await sesion_sin_historial(db, agent_id, session_id):
                return f"{AVISO_CONVERSACION_NUEVA}\n{prompt}"
            if await sesion_recien_reiniciada(db, agent_id, session_id):
                return f"{AVISO_CONVERSACION_REINICIADA}\n{prompt}"
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron listar los archivos generados de la sesión %s", session_id)
        archivos = []
    notas = [n for n in (linea_archivos(archivos), nota_nombrados) if n]
    if not notas:
        return prompt
    return "\n".join([*notas, prompt])
