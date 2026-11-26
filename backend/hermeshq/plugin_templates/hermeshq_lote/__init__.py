from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

MAX_FILES = 10
MAX_CHARS_DEFAULT = 4000
MAX_CHARS_LIMIT = 20000
MAX_PATTERNS = 8
MAX_MATCHES_DEFAULT = 15
MAX_SCANNED_FILES = 3000
MAX_SCANNED_BYTES = 2_000_000
MAX_SKILLS = 6
MAX_SKILL_CHARS = 14000
MAX_EDITS = 20
SKIPPED_DIRS = {".git", "node_modules", "sessions", "__pycache__", ".venv"}


def _workspace() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()


def _skills_root() -> Path:
    home = os.environ.get("HERMES_HOME")
    return (Path(home) / "skills").resolve() if home else _workspace() / ".hermes" / "skills"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _resolve(raw: str) -> Path:
    path = Path(str(raw))
    return (path if path.is_absolute() else _workspace() / path).resolve()


def _readable(path: Path) -> str | None:
    if not (_inside(path, _workspace()) or _inside(path, _skills_root())):
        return "La ruta está fuera de tu carpeta de trabajo."
    if not path.is_file():
        return "No existe o es una carpeta."
    return None


def _read_text(path: Path) -> tuple[str | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"No se pudo leer: {exc.strerror or exc}"
    if b"\x00" in data[:4096]:
        return None, "Es un archivo binario; usa la herramienta específica de ese formato."
    return data.decode("utf-8", errors="replace"), None


def _as_list(value, limit: int, label: str):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        return None, f"Indica {label} como una lista con al menos un elemento."
    value = [item for item in value if item not in (None, "")]
    if not value:
        return None, f"Indica {label} como una lista con al menos un elemento."
    if len(value) > limit:
        return None, f"Máximo {limit} elementos por llamada en {label}; divide la petición."
    return value, None


def _bounded(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _answer(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _read_many_tool(args: dict, **_kwargs) -> str:
    paths, error = _as_list(args.get("paths"), MAX_FILES, "paths")
    if error:
        return _answer({"success": False, "error": error})
    limit = _bounded(args.get("max_chars"), MAX_CHARS_DEFAULT, 200, MAX_CHARS_LIMIT)
    files = []
    for raw in paths:
        path = _resolve(raw)
        problem = _readable(path)
        text = None
        if problem is None:
            text, problem = _read_text(path)
        if problem:
            files.append({"path": str(raw), "error": problem})
            continue
        files.append(
            {
                "path": str(raw),
                "chars": len(text),
                "truncated": len(text) > limit,
                "content": text[:limit],
            }
        )
    return _answer({"success": True, "files": files})


def _iter_files(root: Path, pattern: str | None):
    scanned = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(names):
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            scanned += 1
            if scanned > MAX_SCANNED_FILES:
                return
            yield Path(current) / name


def _search_many_tool(args: dict, **_kwargs) -> str:
    patterns, error = _as_list(args.get("patterns"), MAX_PATTERNS, "patterns")
    if error:
        return _answer({"success": False, "error": error})
    folder = _resolve(args.get("path") or ".")
    if not _inside(folder, _workspace()) or not folder.is_dir():
        return _answer({"success": False, "error": "La carpeta no existe o está fuera de tu carpeta de trabajo."})
    max_matches = _bounded(args.get("max_matches"), MAX_MATCHES_DEFAULT, 1, 100)
    flags = re.IGNORECASE if args.get("ignore_case", True) else 0
    use_regex = bool(args.get("regex"))
    compiled = []
    for pattern in patterns:
        try:
            compiled.append((str(pattern), re.compile(str(pattern) if use_regex else re.escape(str(pattern)), flags)))
        except re.error as exc:
            return _answer({"success": False, "error": f"Patrón inválido '{pattern}': {exc}"})
    results = {name: [] for name, _ in compiled}
    totals = {name: 0 for name, _ in compiled}
    scanned_bytes = 0
    for path in _iter_files(folder, args.get("glob")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 1_000_000:
            continue
        scanned_bytes += size
        if scanned_bytes > MAX_SCANNED_BYTES * 5:
            break
        text, problem = _read_text(path)
        if problem:
            continue
        relative = str(path.relative_to(_workspace()))
        for number, line in enumerate(text.splitlines(), 1):
            for name, regex in compiled:
                if regex.search(line):
                    totals[name] += 1
                    if len(results[name]) < max_matches:
                        results[name].append({"file": relative, "line": number, "text": line.strip()[:200]})
    return _answer(
        {
            "success": True,
            "results": [{"pattern": name, "total": totals[name], "matches": results[name]} for name, _ in compiled],
        }
    )


def _find_skill(name: str) -> Path | None:
    root = _skills_root()
    if not root.is_dir():
        return None
    wanted = str(name).strip().strip("/")
    direct = root / wanted / "SKILL.md"
    if direct.is_file():
        return direct
    for candidate in sorted(root.glob(f"**/{wanted.split('/')[-1]}/SKILL.md")):
        if _inside(candidate, root):
            return candidate
    return None


def _skills_tool(args: dict, **_kwargs) -> str:
    names, error = _as_list(args.get("names"), MAX_SKILLS, "names")
    if error:
        return _answer({"success": False, "error": error})
    skills, missing = [], []
    for name in names:
        path = _find_skill(str(name))
        if path is None:
            missing.append(str(name))
            continue
        text, problem = _read_text(path)
        if problem:
            missing.append(str(name))
            continue
        skills.append(
            {
                "name": str(name),
                "path": str(path.relative_to(_skills_root())),
                "truncated": len(text) > MAX_SKILL_CHARS,
                "content": text[:MAX_SKILL_CHARS],
            }
        )
    payload = {"success": bool(skills), "skills": skills}
    if missing:
        payload["no_encontradas"] = missing
        names_available = (
            sorted({p.parent.name for p in _skills_root().glob("**/SKILL.md")}) if _skills_root().is_dir() else []
        )
        payload["disponibles"] = names_available[:60]
    return _answer(payload)


def _edit_many_tool(args: dict, **_kwargs) -> str:
    path = _resolve(args.get("path") or "")
    if not args.get("path"):
        return _answer({"success": False, "error": "Falta 'path'."})
    if not _inside(path, _workspace()) or not path.is_file():
        return _answer({"success": False, "error": "El archivo no existe o está fuera de tu carpeta de trabajo."})
    edits, error = _as_list(args.get("edits"), MAX_EDITS, "edits")
    if error:
        return _answer({"success": False, "error": error})
    text, problem = _read_text(path)
    if problem:
        return _answer({"success": False, "error": problem})
    updated, problems, applied = text, [], []
    for number, edit in enumerate(edits, 1):
        old, new = (edit.get("old"), edit.get("new")) if isinstance(edit, dict) else (None, None)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            problems.append(f"Edición {number}: necesita 'old' (texto no vacío) y 'new'.")
            continue
        found = updated.count(old)
        if found != 1:
            problems.append(
                f"Edición {number}: el texto 'old' aparece {found} veces (debe aparecer exactamente una); "
                "amplíalo con contexto hasta que sea único."
            )
            continue
        updated = updated.replace(old, new, 1)
        applied.append(number)
    if problems:
        return _answer({"success": False, "error": "No se cambió nada.", "problemas": problems})
    destino = _siguiente_version(path)
    destino.write_text(updated, encoding="utf-8")
    return _answer(
        {
            "success": True,
            "ediciones_aplicadas": len(applied),
            "path": args["path"],
            "archivo_nuevo": str(destino.relative_to(_workspace())),
            "original_intacto": True,
        }
    )


def _siguiente_version(path: Path) -> Path:
    n = 2
    while True:
        candidato = path.with_name(f"{path.stem}_v{n}{path.suffix}")
        if not candidato.exists():
            return candidato
        n += 1


MAX_CAPTURE_PAGES = 6
CAPTURE_DPI_DEFAULT = 70
MAX_FIND_TEXTS = 8


_CLAVES_DE_RUTA = ("path", "archivo", "file_path", "file", "ruta", "html")


def _ruta_pedida(args: dict) -> str:
    for clave in _CLAVES_DE_RUTA:
        valor = args.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return ""


def _html_capture_tool(args: dict, **_kwargs) -> str:
    ruta_pedida = _ruta_pedida(args)
    path = _resolve(ruta_pedida)
    if not ruta_pedida:
        return _answer({"success": False, "error": "Falta 'path' (archivo .html dentro de tu carpeta de trabajo)."})
    if not _inside(path, _workspace()) or not path.is_file():
        return _answer({"success": False, "error": "El archivo no existe o está fuera de tu carpeta de trabajo."})
    try:
        import pymupdf
        import weasyprint
    except ImportError:
        return _answer({"success": False, "error": "No está disponible el motor de captura en este servidor."})
    dpi = _bounded(args.get("dpi"), CAPTURE_DPI_DEFAULT, 40, 150)
    try:
        pdf = weasyprint.HTML(filename=str(path)).write_pdf()
        document = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        return _answer({"success": False, "error": f"No se pudo renderizar el HTML: {str(exc)[:200]}"})
    total = len(document)
    wanted = args.get("pages")
    if isinstance(wanted, int):
        wanted = [wanted]
    if isinstance(wanted, list) and wanted:
        numbers = [n for n in (_bounded(x, 0, 0, total) for x in wanted) if 1 <= n <= total]
    else:
        numbers = list(range(1, min(total, MAX_CAPTURE_PAGES) + 1))
    numbers = list(dict.fromkeys(numbers))[:MAX_CAPTURE_PAGES]
    folder = _workspace() / "work" / "capturas"
    folder.mkdir(parents=True, exist_ok=True)
    images = []
    for number in numbers:
        target = folder / f"{path.stem}-p{number}.png"
        document[number - 1].get_pixmap(dpi=dpi).save(str(target))
        images.append({"page": number, "path": str(target.relative_to(_workspace()))})
    found = {}
    texts = args.get("find") or []
    if isinstance(texts, str):
        texts = [texts]
    for text in [str(t) for t in texts if str(t).strip()][:MAX_FIND_TEXTS]:
        found[text] = [i + 1 for i in range(total) if text.lower() in document[i].get_text().lower()]
    answer = {
        "success": True,
        "pages_total": total,
        "images": images,
        "note": (
            "Es una captura estática (sin JavaScript): lo que la página arma con scripts puede no aparecer. "
            "Revisa las imágenes con una sola llamada a vision_analyze por imagen."
        ),
    }
    if found:
        answer["found_on_pages"] = found
    return _answer(answer)


def register(ctx):
    ctx.register_tool(
        name="hq_leer_varios",
        toolset="hermeshq_lote",
        schema={
            "name": "hq_leer_varios",
            "description": (
                "Lee VARIOS archivos de texto en una sola llamada (hasta 10). Úsala siempre que necesites más de un "
                "archivo, en vez de llamar read_file uno por uno. Devuelve el contenido de cada uno, recortado a "
                "max_chars, o su error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Rutas de los archivos."},
                    "max_chars": {"type": "integer", "description": "Máximo de caracteres por archivo (default 4000)."},
                },
                "required": ["paths"],
            },
        },
        handler=_read_many_tool,
        emoji="📚",
    )
    ctx.register_tool(
        name="hq_buscar_varios",
        toolset="hermeshq_lote",
        schema={
            "name": "hq_buscar_varios",
            "description": (
                "Busca VARIAS palabras o patrones en los archivos de una carpeta en una sola llamada (hasta 8). "
                "Devuelve, por patrón, el total y las primeras coincidencias con archivo y línea."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patterns": {"type": "array", "items": {"type": "string"}},
                    "path": {"type": "string", "description": "Carpeta donde buscar (default: tu carpeta de trabajo)."},
                    "glob": {"type": "string", "description": "Filtro de nombre, p. ej. *.json."},
                    "regex": {"type": "boolean", "description": "true: los patrones son expresiones regulares."},
                    "ignore_case": {"type": "boolean", "description": "Default true."},
                    "max_matches": {
                        "type": "integer",
                        "description": "Máximo de coincidencias por patrón (default 15).",
                    },
                },
                "required": ["patterns"],
            },
        },
        handler=_search_many_tool,
        emoji="🔎",
    )
    ctx.register_tool(
        name="hq_skills_ver",
        toolset="hermeshq_lote",
        schema={
            "name": "hq_skills_ver",
            "description": (
                "Carga VARIAS skills en una sola llamada (hasta 6), por nombre. Úsala en vez de llamar skill_view "
                "una por una. Si algún nombre no existe, devuelve los disponibles."
            ),
            "parameters": {
                "type": "object",
                "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                "required": ["names"],
            },
        },
        handler=_skills_tool,
        emoji="🧩",
    )
    ctx.register_tool(
        name="hq_editar_varios",
        toolset="hermeshq_lote",
        schema={
            "name": "hq_editar_varios",
            "description": (
                "Aplica VARIAS ediciones a UN archivo en una sola llamada (hasta 20). Cada edición es {old, new} y "
                "'old' debe aparecer exactamente una vez. Todo o nada: si una falla, no se cambia nada y se explica cuál."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                            "required": ["old", "new"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
        handler=_edit_many_tool,
        emoji="✏️",
    )
    ctx.register_tool(
        name="hq_html_capturar",
        toolset="hermeshq_lote",
        schema={
            "name": "hq_html_capturar",
            "description": (
                "Convierte un archivo HTML de tu carpeta en imágenes PNG de sus páginas (y, si quieres, dice en qué "
                "página aparece cada texto) en UNA llamada. Úsala para revisar visualmente un informe HTML con "
                "vision_analyze; no pruebes navegadores ni conversores. No ejecuta JavaScript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del .html en tu carpeta de trabajo."},
                    "pages": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Páginas a capturar (default: las primeras 6).",
                    },
                    "find": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Textos cuya página quieres ubicar.",
                    },
                    "dpi": {"type": "integer", "description": "Resolución (default 70, máx 150)."},
                },
                "required": ["path"],
            },
        },
        handler=_html_capture_tool,
        emoji="📸",
    )
