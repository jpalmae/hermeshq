from __future__ import annotations

import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path


def _workspace() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd()))


def _ejecutar(script: Path, argumentos: list[str], cwd: Path | None = None, entorno: dict | None = None):
    return subprocess.run(
        [sys.executable, str(script), *argumentos],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=str(cwd) if cwd else None,
        env=dict(os.environ, HOME=os.environ.get("HOME", "/tmp"), **(entorno or {})),
    )


def _entrada_unica(trabajo: Path, prefijo: str, datos: dict) -> Path:
    trabajo.mkdir(parents=True, exist_ok=True)
    entrada = trabajo / f".{prefijo}_{uuid.uuid4().hex}.json"
    entrada.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return entrada


def _ultima_linea_json(salida: str) -> dict:
    for linea in reversed(salida.strip().splitlines()):
        try:
            return json.loads(linea)
        except ValueError:
            continue
    return {}


_PREFIJO_DESCARGA = re.compile(r"^[0-9a-f]{8}_")


def _dentro_de(base: Path, ruta: Path) -> bool:
    return ruta == base or base in ruta.parents


def _resolver_docx(valor: str, workspace: Path) -> tuple[Path | None, str | None]:
    base = workspace.resolve()
    texto = str(valor or "").strip()
    crudo = Path(texto)
    if crudo.is_absolute() or texto.startswith("~") or ".." in crudo.parts:
        candidato = (crudo if crudo.is_absolute() else base / crudo).resolve()
        if not _dentro_de(base, candidato):
            return None, f"'{texto}' está fuera del espacio de trabajo; solo se editan archivos de tu espacio."
    directo = (base / crudo).resolve()
    if directo.is_file():
        if not _dentro_de(base, directo):
            return None, f"'{texto}' está fuera del espacio de trabajo; solo se editan archivos de tu espacio."
        if directo.suffix.lower() != ".docx":
            return None, f"'{texto}' no es un .docx."
        return directo, None
    coincidencias = []
    for carpeta in (base / "sharepoint_downloads", base):
        if carpeta.is_dir():
            coincidencias += [
                p
                for p in carpeta.iterdir()
                if p.is_file()
                and not p.is_symlink()
                and p.suffix.lower() == ".docx"
                and not p.name.startswith("~$")
                and texto.lower() in p.name.lower()
            ]
    if not coincidencias:
        return None, f"No encontré un .docx que coincida con '{texto}'. Bájalo primero con sharepoint_download_file."
    distintos = sorted({_PREFIJO_DESCARGA.sub("", p.name) for p in coincidencias})
    if len(distintos) > 1:
        return None, (
            f"'{texto}' coincide con varios documentos distintos: {', '.join(distintos[:10])}. "
            "Repite con el nombre exacto o la ruta del que quieres editar."
        )
    coincidencias.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return coincidencias[0].resolve(), None


MAX_VERSIONES_DOCX = 50


def _salidas_candidatas(ruta: Path):
    base, ext = os.path.splitext(ruta.name)
    yield ruta.parent / f"{base}_editado{ext}"
    for n in range(2, MAX_VERSIONES_DOCX + 1):
        yield ruta.parent / f"{base}_editado-{n}{ext}"


def _editar_docx(args, **_kwargs):
    script = Path(__file__).with_name("editar_docx.py")
    operaciones = args.get("operaciones")
    archivo = args.get("archivo")
    if not archivo or not operaciones:
        resultado = _ejecutar(script, ["--info"])
        return json.dumps(
            {
                "success": True,
                "receta": _ultima_linea_json(resultado.stdout).get("receta"),
                "siguiente": "Baja el documento con sharepoint_download_file, luego llama con archivo (ruta o parte del nombre) y operaciones (lista). Después sube el resultado con sharepoint_upload_file. NO vuelques el XML con execute_code.",
            },
            ensure_ascii=False,
        )
    workspace = _workspace()
    ruta, error = _resolver_docx(archivo, workspace)
    if ruta is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    datos = {}
    for salida in _salidas_candidatas(ruta):
        if salida.exists():
            continue
        entrada = _entrada_unica(
            workspace / "work",
            "editar_docx_entrada",
            {
                "archivo": str(ruta),
                "operaciones": operaciones,
                "salida": str(salida),
                "huella_origen": args.get("huella_origen") or None,
            },
        )
        try:
            resultado = _ejecutar(script, [str(entrada)], cwd=workspace)
        except subprocess.TimeoutExpired:
            return json.dumps(
                {"success": False, "error": "La edición del .docx tardó más de 4 minutos."}, ensure_ascii=False
            )
        finally:
            entrada.unlink(missing_ok=True)
        datos = _ultima_linea_json(resultado.stdout)
        if datos.get("error") != "salida_existe":
            break
    else:
        datos = {"ok": False, "error": f"Hay demasiadas versiones editadas de '{ruta.name}'; limpia las antiguas."}
    if not datos.get("ok"):
        if datos.get("error") == "documento_cambio":
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "El documento cambió desde que lo leíste (su huella ya no coincide). No se editó. "
                        "Vuelve a leerlo y decide los cambios sobre la versión actual."
                    ),
                    "huella_actual": datos.get("huella_actual"),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": False, "error": datos.get("error") or (resultado.stderr or resultado.stdout)[-400:]},
            ensure_ascii=False,
        )
    salida_relativa = str(Path(datos["salida"]).resolve().relative_to(workspace.resolve()))
    respuesta = {
        "success": True,
        "salida": salida_relativa,
        "original": str(ruta.relative_to(workspace.resolve())),
        "resumen": datos["resumen"],
        "huella_origen": datos.get("huella_origen"),
        "huella_salida": datos.get("huella_salida"),
        "siguiente": "Se creó una versión NUEVA (el original no se tocó). Súbela a OneDrive con sharepoint_upload_file. Para ajustes de redacción puntuales usa otra pasada con buscar_reemplazar; NO vuelques el XML con execute_code.",
    }
    if datos.get("avisos"):
        respuesta["avisos"] = datos["avisos"]
        no_aplicadas = [
            {"operacion": i + 1, **{k: v for k, v in op.items() if k in ("op", "buscar")}}
            for i, op in enumerate(operaciones)
            if isinstance(op, dict) and any(a.startswith(f"la operacion {i + 1} ") for a in datos["avisos"])
        ]
        respuesta["success"] = False
        respuesta["no_aplicadas"] = no_aplicadas
        if datos.get("sin_cambios"):
            respuesta["error"] = (
                "Ninguna operación encontró nada que cambiar: la versión nueva es igual al original. No la subas "
                "ni digas que el documento cambió."
            )
            respuesta["siguiente"] = (
                "Lee el documento con hq_leer_documento para ver el texto exacto y repite las operaciones con ese "
                "texto en 'buscar' (el texto que HOY dice el documento, no el nuevo)."
            )
        else:
            respuesta["error"] = (
                "Edición INCOMPLETA: estas operaciones no encontraron su texto y NO se aplicaron: "
                + "; ".join(f"#{n['operacion']} buscar «{n.get('buscar', '')}»" for n in no_aplicadas)
                + ". No digas que esos cambios están hechos."
            )
            respuesta["siguiente"] = (
                "Lee la versión nueva con hq_leer_documento, y repite SOLO las operaciones no aplicadas sobre "
                f"'{salida_relativa}' con el texto que hoy dice el documento en 'buscar' (no el valor nuevo)."
            )
    return json.dumps(respuesta, ensure_ascii=False)


GUARDA_CU_STEM = "correo_unif"
GUARDA_CU_TOOLS = ("read_file", "write_file", "patch", "search_files", "terminal", "execute_code")
GUARDA_SKILLS_DIR = ".hermes/skills"
GUARDA_ESCRITURA = (
    "write",
    "'w'",
    '"w"',
    "replace(",
    "rename(",
    "shutil",
    "rm ",
    "mv ",
    "cp ",
    "sed -i",
    " > ",
    ">>",
    "tee ",
    "unlink",
    "remove(",
)
GUARDA_SKILLS_MSG = "el agente no modifica sus skills por su cuenta. Los cambios los pide el usuario a un administrador de la plataforma."


def _ruta_en_espacio(ruta) -> Path:
    candidata = Path(str(ruta or ""))
    return candidata if candidata.is_absolute() else _workspace() / candidata


def _textos(valor):
    if isinstance(valor, str):
        yield valor
    elif isinstance(valor, dict):
        for item in valor.values():
            yield from _textos(item)
    elif isinstance(valor, (list, tuple)):
        for item in valor:
            yield from _textos(item)


GUARDA_EMPAQUETADO = ("zipfile", "zf.", "tarfile", "make_archive", ".zip", ".tar")


def _escribe_en_skills(textos) -> bool:
    for texto in textos:
        plano = str(texto).replace("\\", "/")
        if GUARDA_SKILLS_DIR in plano and any(marca in plano for marca in GUARDA_ESCRITURA):
            if not _solo_empaqueta(plano):
                return True
        if "SKILL.md" in plano:
            for linea in plano.splitlines():
                if "SKILL.md" in linea and any(marca in linea for marca in GUARDA_ESCRITURA):
                    if not any(marca in linea.lower() for marca in GUARDA_EMPAQUETADO):
                        return True
    return False


def _solo_empaqueta(plano: str) -> bool:
    minuscula = plano.lower()
    if not any(marca in minuscula for marca in GUARDA_EMPAQUETADO):
        return False
    for linea in plano.splitlines():
        if GUARDA_SKILLS_DIR in linea and any(marca in linea for marca in GUARDA_ESCRITURA):
            if not any(marca in linea.lower() for marca in GUARDA_EMPAQUETADO):
                return False
    return True


GUARDA_EXTENSIONES_NO_IMAGEN = (".html", ".htm", ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".json", ".md", ".txt")


def _vision_sobre_no_imagen(tool_name, args) -> str | None:
    if tool_name != "vision_analyze":
        return None
    destino = str(args.get("image_url") or args.get("path") or "").split("?")[0].lower()
    if destino.endswith(GUARDA_EXTENSIONES_NO_IMAGEN):
        return (
            "vision_analyze solo mira imágenes (png, jpg, webp). Para ver cómo se ve un HTML, una presentación o un "
            "PDF usa hq_html_capturar (devuelve una imagen de la lámina o página) y analiza esa imagen; para leer su "
            "contenido usa hq_leer_documento. Usa la ruta que devolvió la herramienta que creó el archivo, no una "
            "que imagines."
        )
    return None


GUARDA_COPIA_SKILL_MSG = (
    "ese archivo es una copia de un script de una skill del usuario: el que se ejecuta es el de la skill, no esta copia, "
    "y un trabajo en segundo plano no cambia skills sin que el usuario lo vea. Si el script falla o le falta algo, "
    "termina y explica el problema en tu respuesta: el cambio lo pide el usuario a un administrador de la plataforma."
)


def _scripts_de_skills() -> set[str]:
    try:
        raiz = _casa_hermes() / "skills"
        return {c.name for c in raiz.glob("*/*/scripts/*") if c.is_file() and not c.name.startswith(".")}
    except OSError:
        return set()


def _motivo_skills(tool_name, args, sin_usuario=False):
    args = args if isinstance(args, dict) else {}
    ruta = str(args.get("path") or args.get("file_path") or "").replace("\\", "/")
    if tool_name in ("write_file", "patch"):
        if GUARDA_SKILLS_DIR in ruta:
            return GUARDA_SKILLS_MSG
        if sin_usuario and ruta and Path(ruta).name in _scripts_de_skills():
            return GUARDA_COPIA_SKILL_MSG
    if tool_name in ("execute_code", "terminal") and _escribe_en_skills(list(_textos(args))):
        return GUARDA_SKILLS_MSG
    return None


def _motivo_bloqueo(tool_name, args):
    args = args if isinstance(args, dict) else {}
    vision = _vision_sobre_no_imagen(tool_name, args)
    if vision:
        return vision
    skills = _motivo_skills(tool_name, args, sin_usuario=_es_tarea_sin_usuario())
    if skills:
        return skills
    if tool_name in GUARDA_CU_TOOLS and any(GUARDA_CU_STEM in texto for texto in _textos(args)):
        return (
            "no leas, busques, abras ni modifiques el estado del correo unificado (correo_unificado_estado.json) "
            "a mano ni con scripts. Usa ms365_correo_sync (accion=ventana/plan/commit en el cron; "
            "resolver_id/resolver en chat): hace el trabajo mecánico en el servidor y te devuelve solo lo necesario."
        )
    if tool_name == "delegate_task":
        return _delegacion_de_archivo_propio(args)
    return None


_ARCHIVO_DEL_ESPACIO = re.compile(
    r"\b(sharepoint_downloads|uploads|work|outputs)/[^\n\"']+?\.(docx?|xlsx?|xlsm|csv|pdf|pptx?|html?|txt|md|json)\b",
    re.IGNORECASE,
)


def _delegacion_de_archivo_propio(args) -> str | None:
    texto = json.dumps(args or {}, ensure_ascii=False)
    if not _ARCHIVO_DEL_ESPACIO.search(texto):
        return None
    return (
        "no delegues el análisis de un archivo que ya está en tu espacio de trabajo: léelo tú con hq_leer_documento "
        "y responde tú. Un subagente no tiene tus herramientas de documentos y "
        "solo agrega minutos de espera. No menciones esto al usuario."
    )


def _registrar_bloqueo(tool_name, args, motivo):
    try:
        carpeta = _workspace() / "work"
        carpeta.mkdir(parents=True, exist_ok=True)
        linea = {
            "ts": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool_name,
            "motivo": motivo,
            "path": str((args or {}).get("path") or (args or {}).get("name") or "")[:120],
            "bytes": len(str((args or {}).get("content") or (args or {}).get("code") or "")),
        }
        with open(carpeta / ".bloqueos.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _es_trabajo_en_segundo_plano() -> bool:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return False
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    return bool(isinstance(metadata, dict) and metadata.get("background_job"))


def _es_tarea_sin_usuario() -> bool:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return False
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("background_job")
        or metadata.get("scheduled")
        or metadata.get("scheduled_task_id")
        or metadata.get("source") in ("background_job", "scheduled")
    )


CONSULTAS_VIGILADAS = ("ms365_mail_list", "ms365_calendar_list_events")
LIMITE_CONSULTAS_CORREO = 24
LIMITE_REPETIDAS = 2
_CONSULTAS_DE_LA_TAREA: dict = {}
CONSULTAS_MSG = (
    "ya hiciste esa consulta {n} veces en esta tarea o llevas demasiadas consultas de correo ({total}). Cada consulta "
    "tarda unos 7 segundos y la tarea se corta a los 570 s: usa lo que ya obtuviste. Si el usuario solo pide cambiar "
    "el formato de un informe ya hecho, no vuelvas a consultar correo ni calendario: corrige work/.../spec.json y "
    "genera de nuevo. No menciones esto al usuario."
)


def _motivo_consulta_repetida(tool_name, args):
    if tool_name not in CONSULTAS_VIGILADAS or not isinstance(args, dict):
        return None
    try:
        tarea = str(json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}").get("task_id") or "")
    except ValueError:
        tarea = ""
    registro = _CONSULTAS_DE_LA_TAREA.setdefault(tarea or "sin-tarea", {"llaves": {}, "correo": 0})
    llave = tool_name + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    veces = registro["llaves"].get(llave, 0) + 1
    registro["llaves"][llave] = veces
    if tool_name == "ms365_mail_list":
        registro["correo"] += 1
    if veces > LIMITE_REPETIDAS or registro["correo"] > LIMITE_CONSULTAS_CORREO:
        return CONSULTAS_MSG.format(n=veces - 1, total=registro["correo"])
    return None


def _guarda_pre_tool_call(tool_name=None, args=None, **_kwargs):
    if os.environ.get("HERMESHQ_GUARDA") == "off":
        return None
    repetida = _motivo_consulta_repetida(tool_name, args)
    if repetida:
        _registrar_bloqueo(tool_name, args, repetida)
        return {"action": "block", "message": "Bloqueado: " + repetida}
    if _es_trabajo_en_segundo_plano():
        if tool_name not in ("skill_manage", "delegate_task"):
            motivo = _motivo_skills(tool_name, args, sin_usuario=True)
            if motivo:
                _registrar_bloqueo(tool_name, args, motivo)
                return {"action": "block", "message": "Bloqueado: " + motivo}
            return None
    motivo = _motivo_bloqueo(tool_name, args)
    if not motivo:
        return None
    _registrar_bloqueo(tool_name, args, motivo)
    return {"action": "block", "message": "Bloqueado: " + motivo}


def _casa_hermes() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("HERMES_HOME") or (_workspace() / ".hermes"))


def _check_requirements():
    try:
        import docx  # noqa: F401
        import pptx  # noqa: F401

        return True
    except ImportError:
        return False


DESC_EDITAR_DOCX = (
    "Edita de forma MECÁNICA un .docx EXISTENTE (p. ej. uno bajado de OneDrive/SharePoint), en el servidor, SIN volcar su "
    "contenido al chat. Úsala para: quitar todo el texto en color/resaltado rojo, eliminar comentarios (todos o solo los "
    "resueltos), convertir viñetas/numeración en párrafos, reemplazar texto puntual, agregar un párrafo o una sección "
    "nueva, eliminar un párrafo o eliminar una tabla. Crea una versión NUEVA (el original "
    "no se toca) que luego subes con sharepoint_upload_file. NUNCA edites un .docx a mano con execute_code/python-docx "
    "volcando el XML: satura el contexto y se cuelga; usa SIEMPRE esta herramienta. Flujo: sharepoint_download_file → "
    "hq_docx_editar → sharepoint_upload_file. Sin 'archivo'/'operaciones' devuelve la receta de operaciones disponibles."
)
DESC_OPERACIONES_DOCX = (
    "Lista de operaciones a aplicar EN ORDEN. Cada una es un objeto {op, ...}: "
    "{op:'eliminar_color', colores?:['EE0000'] (por defecto detecta rojos), incluir_resaltado?:true, eliminar_parrafo_vacio?:true} · "
    "{op:'quitar_comentarios', cuales:'todos'|'resueltos'} · "
    "{op:'listas_a_parrafos'} · "
    "{op:'buscar_reemplazar', buscar:'...', reemplazar:'...', parrafo_contiene?:'texto del párrafo a cambiar, p. ej. el nombre de la persona', todas?:true, max?:int} (si «buscar» aparece más de una vez se rechaza aunque pongas max: max NO elige cuál cambiar; para UNA coincidencia concreta usa parrafo_contiene con lo que distingue su párrafo, p. ej. «María Rojas»; todas:true solo si pidieron cambiar todas) · "
    "{op:'agregar_parrafo', texto:'...', despues_de?:'texto de un párrafo existente' (por defecto al final), estilo?:'Heading1'} (una sección con título son DOS operaciones: el título con estilo 'Heading1' y el cuerpo con despues_de = ese título) · "
    "{op:'eliminar_parrafo', contiene:'texto del párrafo', max?:int (por defecto 1)} (quita solo el párrafo, NUNCA una tabla) · "
    "{op:'eliminar_tabla', numero?:int (1 = la primera tabla del documento) | contiene?:'texto de la tabla'} · "
    "{op:'cambiar_celda', tabla?:int | contiene?:'texto de la tabla', fila:'texto de la primera celda de la fila', columna:'encabezado o número', valor:'texto nuevo'} "
    "(para cambiar el valor de UNA celda, p. ej. el Estado de una tarea; NO uses buscar_reemplazar con el nombre de la fila: cambiaría también otras frases)."
)


LIMITE_LECTURA_BYTES = 25 * 1024 * 1024
DESC_LEER_DOCUMENTO = (
    "Lee el texto de un PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx) o archivo de texto/CSV que ya está en tu "
    "espacio de trabajo (adjuntos del usuario, descargas de SharePoint, archivos generados) en UNA llamada. Úsala en "
    "vez de execute_code, pymupdf o pypdf para leer documentos. En un PDF largo lee por partes con desde_pagina y "
    "hasta_pagina."
)


def _leer_documento(args: dict, **_kwargs) -> str:

    crudo = str(args.get("path") or "").strip()
    if not crudo:
        return json.dumps({"success": False, "error": "Falta 'path' (el archivo a leer)."}, ensure_ascii=False)
    espacio = _workspace().resolve()
    ruta = _ruta_en_espacio(crudo).resolve()
    if ruta != espacio and espacio not in ruta.parents:
        return json.dumps(
            {"success": False, "error": f"{crudo} está fuera del espacio de trabajo."}, ensure_ascii=False
        )
    if not ruta.is_file():
        return json.dumps({"success": False, "error": f"No existe el archivo {crudo}."}, ensure_ascii=False)
    if ruta.stat().st_size > LIMITE_LECTURA_BYTES:
        return json.dumps(
            {"success": False, "error": f"El archivo pesa más de {LIMITE_LECTURA_BYTES // (1024 * 1024)} MB."},
            ensure_ascii=False,
        )

    def _entero(valor, defecto, minimo, maximo):
        try:
            return max(minimo, min(maximo, int(valor)))
        except (TypeError, ValueError):
            return defecto

    spec = importlib.util.spec_from_file_location("hq_lector", Path(__file__).with_name("lector.py"))
    lector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lector)
    try:
        resultado = lector.leer(
            ruta,
            max_caracteres=_entero(args.get("max_caracteres"), 30_000, 1_000, 100_000),
            desde_pagina=_entero(args.get("desde_pagina"), 1, 1, 100_000),
            hasta_pagina=_entero(args.get("hasta_pagina"), None, 1, 100_000) if args.get("hasta_pagina") else None,
        )
    except ImportError as exc:
        resultado = {"success": False, "error": f"Falta una librería para leer {ruta.suffix}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        resultado = {"success": False, "error": f"No se pudo leer {ruta.name} (¿dañado o con contraseña?): {exc}"}
    return json.dumps(resultado, ensure_ascii=False)


def register(ctx):
    registrar_gancho = getattr(ctx, "register_hook", None)
    if callable(registrar_gancho):
        registrar_gancho("pre_tool_call", _guarda_pre_tool_call)
    ctx.register_tool(
        name="hq_leer_documento",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_leer_documento",
            "description": DESC_LEER_DOCUMENTO,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta del archivo en el espacio de trabajo (ej. uploads/contrato.pdf).",
                    },
                    "desde_pagina": {"type": "integer", "description": "PDF: primera página a leer (por defecto 1)."},
                    "hasta_pagina": {
                        "type": "integer",
                        "description": "PDF: última página a leer (por defecto la última).",
                    },
                    "max_caracteres": {
                        "type": "integer",
                        "description": "Máximo de caracteres devueltos (por defecto 30000).",
                    },
                },
                "required": ["path"],
            },
        },
        handler=_leer_documento,
        check_fn=_check_requirements,
        description=DESC_LEER_DOCUMENTO,
        emoji="📖",
    )
    ctx.register_tool(
        name="hq_docx_editar",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_docx_editar",
            "description": DESC_EDITAR_DOCX,
            "parameters": {
                "type": "object",
                "properties": {
                    "archivo": {
                        "type": "string",
                        "description": "Ruta o parte del nombre del .docx ya bajado al espacio de trabajo (p. ej. en sharepoint_downloads/).",
                    },
                    "operaciones": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": DESC_OPERACIONES_DOCX,
                    },
                    "huella_origen": {
                        "type": "string",
                        "description": "Opcional: huella sha256 del documento que revisaste (huella_salida de una edición anterior). Si el archivo cambió desde entonces, no se edita.",
                    },
                },
                "required": [],
            },
        },
        handler=_editar_docx,
        check_fn=_check_requirements,
        description=DESC_EDITAR_DOCX,
        emoji="✂️",
    )
