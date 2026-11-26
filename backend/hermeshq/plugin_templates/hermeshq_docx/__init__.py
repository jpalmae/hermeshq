from __future__ import annotations

import json
import os
from pathlib import Path

_BLOCK_TYPES = {
    "heading1",
    "heading2",
    "heading3",
    "paragraph",
    "bullet_list",
    "numbered_list",
    "table",
    "image",
    "page_break",
}

_SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _check_requirements():
    try:
        import docx  # noqa: F401

        return True
    except ImportError:
        return False


def _safe_filename(title: str, ext: str) -> str:
    safe_title = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in title.lower())[:60].strip("-")
    return f"{safe_title or 'documento'}.{ext}"


def _espacio() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd())).resolve()


def _resolve_local_path(raw_path: str) -> Path:
    base = _espacio()
    p = Path(str(raw_path))
    ruta = (p if p.is_absolute() else base / p).resolve()
    if ruta != base and base not in ruta.parents:
        raise ValueError(f"'{raw_path}' está fuera del espacio de trabajo; usa archivos de tu espacio.")
    return ruta


def _nombre_seguro(nombre: str, ext: str, por_defecto: str) -> str:
    base = os.path.basename(str(nombre or "").replace("\\", "/")).strip()
    if base.lower().endswith("." + ext):
        base = base[: -(len(ext) + 1)]
    limpio = "".join(c if c.isalnum() or c in ("-", "_", ".") else "-" for c in base).strip(".-")[:80]
    return f"{limpio or por_defecto}.{ext}"


def _reservar_salida(carpeta: Path, nombre: str) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    raiz, ext = os.path.splitext(nombre)
    for n in range(1, 1000):
        candidato = carpeta / (nombre if n == 1 else f"{raiz}-{n}{ext}")
        try:
            os.close(os.open(candidato, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return candidato
        except FileExistsError:
            continue
    raise ValueError("Hay demasiadas versiones con ese nombre; usa otro filename.")


def _guardar_seguro(destino: Path, escribir, verificar) -> None:
    temporal = destino.with_name(f".tmp-{os.getpid()}-{destino.name}")
    try:
        escribir(temporal)
        verificar(temporal)
        os.replace(temporal, destino)
    except BaseException:
        temporal.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)
        raise


def _verificar_ooxml(ruta: Path) -> None:
    import zipfile

    with zipfile.ZipFile(ruta) as z:
        if z.testzip() is not None or "[Content_Types].xml" not in z.namelist():
            raise ValueError("el archivo generado está dañado")


_ALTO_MAXIMO_IMAGEN_PULGADAS = 4.5


def _add_image(doc, block: dict) -> None:
    from docx.shared import Inches, Pt

    raw_path = str(block.get("path") or "").strip()
    if not raw_path:
        raise ValueError("image block is missing 'path'")
    resolved = _resolve_local_path(raw_path)
    if not resolved.is_file():
        raise ValueError(f"image not found: {raw_path}")
    if resolved.suffix.lower() not in _SUPPORTED_IMAGE_EXTS:
        raise ValueError(
            f"unsupported image format '{resolved.suffix}' for {raw_path}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTS))}"
        )
    width_inches = block.get("width_inches")
    try:
        width_inches = float(width_inches) if width_inches is not None else 6.0
    except (TypeError, ValueError):
        width_inches = 6.0
    width_inches = max(0.5, min(width_inches, 6.5))
    doc.add_picture(str(resolved), width=Inches(width_inches))
    forma = doc.inline_shapes[-1]
    alto_maximo = Inches(_ALTO_MAXIMO_IMAGEN_PULGADAS)
    if forma.height > alto_maximo:
        escala = alto_maximo / forma.height
        forma.width = int(forma.width * escala)
        forma.height = int(alto_maximo)
    imagen = doc.paragraphs[-1]
    imagen.alignment = 1  # WD_ALIGN_PARAGRAPH.CENTER
    imagen.paragraph_format.space_before = Pt(12)
    caption = str(block.get("caption") or "").strip()
    imagen.paragraph_format.keep_with_next = bool(caption)
    if caption:
        caption_para = doc.add_paragraph(caption)
        caption_para.alignment = 1  # WD_ALIGN_PARAGRAPH.CENTER
        for run in caption_para.runs:
            run.italic = True


def _add_table(doc, block: dict) -> None:
    headers = [str(h) for h in (block.get("headers") or [])]
    rows = [r for r in (block.get("rows") or []) if isinstance(r, list)]
    ncols = len(headers) if headers else (len(rows[0]) if rows else 0)
    if not ncols:
        return
    total_rows = (1 if headers else 0) + len(rows)
    if not total_rows:
        return
    table = doc.add_table(rows=total_rows, cols=ncols)
    table.style = "Light Grid Accent 1"
    r = 0
    if headers:
        for i, h in enumerate(headers):
            table.cell(r, i).text = h
        r += 1
    for row in rows:
        for i in range(ncols):
            table.cell(r, i).text = str(row[i]) if i < len(row) else ""
        r += 1


def _leer_secciones(sections):
    if isinstance(sections, list) and len(sections) == 1 and isinstance(sections[0], str):
        sections = sections[0]
    if isinstance(sections, str):
        try:
            sections = json.loads(sections)
        except ValueError as exc:
            return None, (
                f"«sections» llegó como texto y no es JSON válido ({exc}). Envía una lista de objetos "
                '[{"type": "heading1", "text": "…"}, …] (sin comillas alrededor de la lista, comillas dobles '
                "escapadas dentro de los textos) y vuelve a llamar."
            )
    if isinstance(sections, dict):
        sections = [sections]
    return sections, None


def _handle_generate_docx(args, **_kwargs):
    title = str(args.get("title") or "Documento").strip()
    sections, error_secciones = _leer_secciones(args.get("sections"))
    filename = str(args.get("filename") or "").strip()
    if error_secciones:
        return json.dumps({"success": False, "error": error_secciones}, ensure_ascii=False)

    try:
        from docx import Document
    except ImportError:
        return json.dumps(
            {"success": False, "error": "python-docx is not installed. Word document generation is unavailable."}
        )

    if not isinstance(sections, list) or not sections:
        return json.dumps({"success": False, "error": "sections must be a non-empty list of content blocks"})

    if not filename:
        filename = _safe_filename(title, "docx")
    elif not filename.lower().endswith(".docx"):
        filename += ".docx"

    work_dir = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd()))
    output_dir = work_dir / "output"
    filename = _nombre_seguro(filename, "docx", "documento")

    try:
        doc = Document()
        doc.add_heading(title, level=0)
        omitidos = []
        dibujados = 0
        for posicion, block in enumerate(sections, start=1):
            if not isinstance(block, dict):
                omitidos.append(f"bloque {posicion}: no es un objeto")
                continue
            btype = str(block.get("type", "")).strip()
            if btype not in _BLOCK_TYPES:
                omitidos.append(f"bloque {posicion}: tipo '{btype}' desconocido")
                continue
            if btype in ("heading1", "heading2", "heading3"):
                doc.add_heading(str(block.get("text", "")), level=int(btype[-1]))
            elif btype == "paragraph":
                doc.add_paragraph(str(block.get("text", "")))
            elif btype == "bullet_list":
                for item in block.get("items") or []:
                    doc.add_paragraph(str(item), style="List Bullet")
            elif btype == "numbered_list":
                for item in block.get("items") or []:
                    doc.add_paragraph(str(item), style="List Number")
            elif btype == "table":
                _add_table(doc, block)
            elif btype == "image":
                _add_image(doc, block)
            elif btype == "page_break":
                doc.add_page_break()
            dibujados += 1
        if not dibujados:
            return json.dumps(
                {
                    "success": False,
                    "error": f"ningún bloque se pudo dibujar ({'; '.join(omitidos[:5])}); no se creó el documento. "
                    "Corrige «sections» (lista de objetos con «type») y vuelve a llamar.",
                },
                ensure_ascii=False,
            )
        filepath = _reservar_salida(output_dir, filename)
        _guardar_seguro(filepath, lambda t: doc.save(str(t)), lambda t: (_verificar_ooxml(t), Document(str(t))))
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Word document generation failed: {exc}"})

    resultado = {
        "success": True,
        "filename": filepath.name,
        "path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "adjuntar_en_la_respuesta": _linea_media(filepath),
    }
    if omitidos:
        resultado["bloques_omitidos"] = omitidos
        resultado["aviso"] = (
            f"Se omitieron {len(omitidos)} bloques que no se pudieron dibujar ({'; '.join(omitidos[:5])}). "
            "Dile al usuario qué faltó o corrige y vuelve a generar."
        )
    return json.dumps(resultado, ensure_ascii=False)


def _linea_media(ruta: Path) -> list[str]:
    base = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd())).resolve()
    try:
        return [f"MEDIA:{ruta.resolve().relative_to(base)}"]
    except ValueError:
        return [f"MEDIA:{ruta}"]


def _cargar_versiones():
    import importlib.util

    ruta = Path(__file__).resolve().parent / "versiones.py"
    spec = importlib.util.spec_from_file_location("hq_versiones_impl", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _handle_versiones(args, **_kwargs):
    nombre = str(args.get("documento") or "").strip()
    if not nombre:
        return json.dumps(
            {"success": False, "error": "Falta 'documento' (nombre del archivo, por ejemplo informe.docx)."},
            ensure_ascii=False,
        )
    try:
        modulo = _cargar_versiones()
        espacio = _espacio()
        version = args.get("restaurar_version")
        if version in (None, ""):
            versiones = modulo.listar(espacio, nombre)
            if not versiones:
                return json.dumps(
                    {"success": False, "error": f"No encontré versiones de «{nombre}» en tu espacio de trabajo."},
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "success": True,
                    "documento": modulo.base_de(nombre),
                    "versiones": versiones,
                    "siguiente": "Las versiones van de la más antigua a la más reciente; 'agrega' y 'quita' dicen qué cambió respecto de la anterior. Para volver a una, llama de nuevo con restaurar_version=N y entrega el archivo con MEDIA.",
                },
                ensure_ascii=False,
            )
        destino, error = modulo.restaurar(espacio, nombre, int(version))
        if error:
            return json.dumps({"success": False, "error": error}, ensure_ascii=False)
        relativo = str(destino.relative_to(espacio))
        return json.dumps(
            {
                "success": True,
                "restaurada": int(version),
                "path": relativo,
                "siguiente": f"Entrega el archivo con la línea MEDIA:{relativo}. No tocó ninguna versión existente.",
            },
            ensure_ascii=False,
        )
    except (ValueError, OSError) as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def register(ctx):
    spec = {
        "name": "hq_docx_generate",
        "description": (
            "Generate a Word (.docx) document from structured content blocks. "
            "Supports headings, paragraphs, bullet/numbered lists, tables, embedded images, and page breaks. "
            "The file is saved to the agent's output directory and can be attached to messages. "
            "ALWAYS use this tool for Word files instead of python-docx scripts with execute_code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title (shown as the top-level heading and used for the default filename)",
                },
                "sections": {
                    "type": "array",
                    "description": "Ordered list of content blocks that make up the document body",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": sorted(_BLOCK_TYPES),
                                "description": "Block type",
                            },
                            "text": {
                                "type": "string",
                                "description": "Text content — used by heading1/heading2/heading3/paragraph blocks",
                            },
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List items — used by bullet_list/numbered_list blocks",
                            },
                            "headers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional table column headers — used by table blocks",
                            },
                            "rows": {
                                "type": "array",
                                "items": {"type": "array", "items": {"type": "string"}},
                                "description": "Table rows, each a list of cell values — used by table blocks",
                            },
                            "path": {
                                "type": "string",
                                "description": (
                                    "Path to an existing local image file (png/jpg/jpeg/gif/bmp), relative to the "
                                    "workspace or absolute — used by image blocks. The file must already exist "
                                    "(e.g. uploaded by the user or produced by another tool); this does not fetch URLs."
                                ),
                            },
                            "width_inches": {
                                "type": "number",
                                "description": "Optional image display width in inches (0.5–6.5, default 6.0) — used by image blocks",
                            },
                            "caption": {
                                "type": "string",
                                "description": "Optional italic caption shown centered below the image — used by image blocks",
                            },
                        },
                        "required": ["type"],
                    },
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename (e.g. 'informe.docx'). If omitted, derived from title.",
                },
            },
            "required": ["title", "sections"],
        },
        "handler": _handle_generate_docx,
        "emoji": "📝",
    }

    ctx.register_tool(
        name=spec["name"],
        toolset="hermeshq_docx",
        schema={
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
        handler=spec["handler"],
        check_fn=_check_requirements,
        description=spec["description"],
        emoji=spec["emoji"],
    )

    ctx.register_tool(
        name="hq_versiones_documento",
        toolset="hermeshq_docx",
        schema={
            "name": "hq_versiones_documento",
            "description": (
                "Lista las versiones de un documento (Word, PowerPoint o Excel) de tu espacio de trabajo, de la más "
                "antigua a la más reciente, con la fecha y lo que cada una agregó o quitó respecto de la anterior. "
                "Úsala cuando el usuario pida «vuelve a la versión anterior», «la versión sin el anexo» o «qué cambió»: "
                "NO adivines por los nombres de archivo. Con restaurar_version=N copia esa versión a un archivo nuevo "
                "(_restaurada) sin tocar ninguna existente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "documento": {
                        "type": "string",
                        "description": "Nombre del archivo o de cualquiera de sus versiones.",
                    },
                    "restaurar_version": {
                        "type": "integer",
                        "description": "Número de versión a restaurar (opcional).",
                    },
                },
                "required": ["documento"],
            },
        },
        handler=_handle_versiones,
        check_fn=_check_requirements,
        description="Versiones de un documento: lista con cambios y restauración.",
        emoji="🕘",
    )
