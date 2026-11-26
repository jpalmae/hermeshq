from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _check_requirements():
    try:
        import pptx  # noqa: F401

        return True
    except ImportError:
        return False


def _safe_filename(title: str, ext: str) -> str:
    safe_title = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in title.lower())[:60].strip("-")
    return f"{safe_title or 'presentacion'}.{ext}"


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


def _verificar_pptx(ruta: Path) -> None:
    from pptx import Presentation

    _verificar_ooxml(ruta)
    _asegurar_respaldo_svg(ruta)
    Presentation(str(ruta))


_SVG_BLIP = "{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip"


def _png_desde_svg(svg: bytes) -> bytes | None:
    try:
        import pymupdf

        pagina = pymupdf.open(stream=svg, filetype="svg")[0]
        escala = max(1.0, 512 / max(pagina.rect.width, 1.0))
        return pagina.get_pixmap(matrix=pymupdf.Matrix(escala, escala), alpha=True).tobytes("png")
    except Exception:  # noqa: BLE001
        try:
            import cairosvg

            return cairosvg.svg2png(bytestring=svg, output_width=512)
        except Exception:  # noqa: BLE001
            return None


def _asegurar_respaldo_svg(ruta: Path) -> None:
    from io import BytesIO

    from pptx import Presentation
    from pptx.oxml.ns import qn

    prs = Presentation(str(ruta))
    cambios = 0
    contenedores = list(prs.slides) + [m for maestro in prs.slide_masters for m in (maestro, *maestro.slide_layouts)]
    for contenedor in contenedores:
        for blip in list(contenedor._element.iter(qn("a:blip"))):
            svg = next(blip.iter(_SVG_BLIP), None)
            if svg is None or blip.get(qn("r:embed")):
                continue
            try:
                png = _png_desde_svg(contenedor.part.related_part(svg.get(qn("r:embed"))).blob)
            except (KeyError, AttributeError):
                png = None
            if png:
                _, rid = contenedor.part.get_or_add_image_part(BytesIO(png))
                blip.set(qn("r:embed"), rid)
            else:
                imagen = blip.getparent().getparent()
                if imagen is not None and imagen.tag == qn("p:pic") and imagen.getparent() is not None:
                    imagen.getparent().remove(imagen)
            cambios += 1
    if cambios:
        prs.save(str(ruta))


def _resolve_slide_image(image_path: str) -> Path:
    resolved = _resolve_local_path(image_path)
    if not resolved.is_file():
        raise ValueError(f"image not found: {image_path}")
    if resolved.suffix.lower() not in _SUPPORTED_IMAGE_EXTS:
        raise ValueError(
            f"unsupported image format '{resolved.suffix}' for {image_path}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTS))}"
        )
    return resolved


def _add_slide_image(prs, slide, resolved_image: Path, width_inches: float | None, body_ph) -> None:
    from pptx.util import Inches

    if body_ph is not None:
        # Text is present — shrink its placeholder to the left half and put
        # the image in the remaining right half so they don't overlap.
        body_ph.width = int(body_ph.width * 0.5)
        left = body_ph.left + body_ph.width + Inches(0.3)
        top = body_ph.top
        max_width = prs.slide_width - left - Inches(0.3)
        width = Inches(width_inches) if width_inches else max_width
        width = min(width, max_width)
        slide.shapes.add_picture(str(resolved_image), left, top, width=int(width))
    else:
        # No text placeholder in use — center the image below the title.
        width = Inches(min(width_inches, 8.5)) if width_inches else Inches(6.0)
        width = min(width, prs.slide_width - Inches(1.0))
        left = int((prs.slide_width - width) / 2)
        top = Inches(1.8)
        slide.shapes.add_picture(str(resolved_image), left, top, width=int(width))


def _faltan_laminas_pedidas(slides: list) -> str | None:
    try:
        from .laminas_pedidas import _laminas_pedidas
    except ImportError:
        import importlib.util

        ruta = Path(__file__).with_name("laminas_pedidas.py")
        spec = importlib.util.spec_from_file_location("hq_pptx_laminas_pedidas", ruta)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        _laminas_pedidas = modulo._laminas_pedidas
    pedidas = _laminas_pedidas()
    if not pedidas:
        return None
    numero, frase, modo = pedidas
    contenido = len(slides)
    totales = contenido + 1
    if modo == "maximo":
        correcto = totales <= numero or contenido <= numero
    elif modo == "minimo":
        correcto = contenido >= numero or totales >= numero
    else:
        correcto = numero in (contenido, totales)
    if correcto:
        return None
    return (
        f"El usuario pidió {numero} diapositivas («{frase}») y 'slides' trae {contenido} de contenido (la portada la "
        f"agrega la herramienta: serían {totales} en total). Revisa su mensaje, ajusta la lista a lo que pidió y vuelve "
        "a llamar; no digas una cantidad distinta de la que devuelva 'slide_count'."
    )


def _handle_generate_pptx(args, **_kwargs):
    title = str(args.get("title") or "Presentación").strip()
    subtitle = str(args.get("subtitle") or "").strip()
    slides = args.get("slides")
    filename = str(args.get("filename") or "").strip()

    try:
        from pptx import Presentation
    except ImportError:
        return json.dumps(
            {"success": False, "error": "python-pptx is not installed. PowerPoint generation is unavailable."}
        )

    if not isinstance(slides, list) or not slides:
        return json.dumps({"success": False, "error": "slides must be a non-empty list of slide objects"})
    invalidas = [i + 1 for i, s in enumerate(slides) if not isinstance(s, dict)]
    if invalidas:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Los elementos {', '.join(map(str, invalidas))} de 'slides' no son objetos {{\"title\": ..., "
                    '"bullets": [...]}}: no se habrían dibujado y la presentación saldría con menos láminas de las '
                    "que declaraste. Corrige la lista y vuelve a llamar."
                ),
            },
            ensure_ascii=False,
        )
    faltan = _faltan_laminas_pedidas(slides)
    if faltan:
        return json.dumps({"success": False, "error": faltan}, ensure_ascii=False)

    if not filename:
        filename = _safe_filename(title, "pptx")
    elif not filename.lower().endswith(".pptx"):
        filename += ".pptx"

    work_dir = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd()))
    output_dir = work_dir / "output"
    filename = _nombre_seguro(filename, "pptx", "presentacion")

    try:
        prs = Presentation()
        title_slide = prs.slides.add_slide(prs.slide_layouts[0])
        if title_slide.shapes.title:
            title_slide.shapes.title.text = title
        if subtitle and len(title_slide.placeholders) > 1:
            title_slide.placeholders[1].text = subtitle

        content_layout = prs.slide_layouts[1]
        section_layout = prs.slide_layouts[2] if len(prs.slide_layouts) > 2 else content_layout

        diapositivas_creadas = 1
        for slide_spec in slides:
            if not isinstance(slide_spec, dict):
                continue
            diapositivas_creadas += 1
            layout = section_layout if str(slide_spec.get("layout", "")).strip() == "section" else content_layout
            slide = prs.slides.add_slide(layout)
            if slide.shapes.title:
                slide.shapes.title.text = str(slide_spec.get("title", ""))
            bullets_crudos = slide_spec.get("bullets") or []
            if isinstance(bullets_crudos, str):
                bullets_crudos = [bullets_crudos]
            elif not isinstance(bullets_crudos, (list, tuple)):
                bullets_crudos = [str(bullets_crudos)]
            bullets = [str(b) for b in bullets_crudos if str(b).strip()]
            image_path = str(
                slide_spec.get("image_path") or slide_spec.get("image") or slide_spec.get("imagen") or ""
            ).strip()
            body_ph = None
            if bullets and len(slide.placeholders) > 1:
                body_ph = slide.placeholders[1]
                text_frame = body_ph.text_frame
                text_frame.clear()
                text_frame.text = bullets[0]
                for bullet in bullets[1:]:
                    paragraph = text_frame.add_paragraph()
                    paragraph.text = bullet
            elif not image_path and len(slide.placeholders) > 1:
                vacio = slide.placeholders[1]._element
                vacio.getparent().remove(vacio)
            if image_path:
                resolved_image = _resolve_slide_image(image_path)
                width_inches = slide_spec.get("image_width_inches")
                try:
                    width_inches = float(width_inches) if width_inches is not None else None
                except (TypeError, ValueError):
                    width_inches = None
                _add_slide_image(prs, slide, resolved_image, width_inches, body_ph)
            notes = str(slide_spec.get("notes") or "").strip()
            if notes:
                slide.notes_slide.notes_text_frame.text = notes

        filepath = _reservar_salida(output_dir, filename)
        _guardar_seguro(filepath, lambda t: prs.save(str(t)), _verificar_pptx)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"PowerPoint generation failed: {exc}"})

    return json.dumps(
        {
            "success": True,
            "filename": filepath.name,
            "path": str(filepath),
            "size_bytes": filepath.stat().st_size,
            "slide_count": diapositivas_creadas,
            "resumen": (
                f"Presentación con {diapositivas_creadas} diapositivas (1 portada + {diapositivas_creadas - 1} de "
                "contenido). Informa esa cantidad."
            ),
            "adjuntar_en_la_respuesta": _linea_media(filepath),
        }
    )


def _load_template_module():
    import importlib.util

    ruta = Path(__file__).with_name("plantilla.py")
    spec = importlib.util.spec_from_file_location("hq_pptx_plantilla", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _handle_from_template(args, **_kwargs):
    template = str(args.get("template") or "").strip()
    if not template:
        return json.dumps(
            {"success": False, "error": "Indica 'template': la ruta de la plantilla .pptx."}, ensure_ascii=False
        )
    try:
        modulo = _load_template_module()
        plantilla = _resolve_local_path(template)
        if args.get("inspect") or not args.get("slides"):
            return json.dumps(
                {
                    "success": True,
                    "plantilla": plantilla.name,
                    **modulo.inspeccionar(plantilla),
                    "siguiente": "Elige un diseño por nombre o índice para cada lámina y llama de nuevo con slides.",
                },
                ensure_ascii=False,
            )
        filename = _nombre_seguro(
            str(args.get("filename") or "").strip() or plantilla.stem + "-presentacion", "pptx", "presentacion"
        )
        work_dir = _espacio()
        destino = _reservar_salida(work_dir / "output", filename)
        resultado = {}

        def _escribir(temporal):
            resultado.update(
                modulo.construir(
                    plantilla, args.get("slides"), temporal, bool(args.get("keep_template_slides")), work_dir
                )
            )

        _guardar_seguro(destino, _escribir, _verificar_pptx)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001  # un fallo de python-pptx no debe tumbar la tarea
        return json.dumps(
            {"success": False, "error": f"No se pudo generar la presentación: {str(exc)[:200]}"}, ensure_ascii=False
        )
    return json.dumps(
        {
            "success": True,
            "filename": destino.name,
            "path": str(destino),
            "size_bytes": destino.stat().st_size,
            "adjuntar_en_la_respuesta": _linea_media(destino),
            "slide_count": resultado["laminas"],
            "disenos_usados": resultado["disenos_usados"],
            "avisos": resultado["avisos"],
        },
        ensure_ascii=False,
    )


DESC_FROM_TEMPLATE = (
    "Genera una presentación PowerPoint usando los DISEÑOS de una plantilla .pptx propia del usuario (colores, "
    "tipografías, logos y márgenes ya definidos), en una sola llamada y sin escribir código. Primero llámala con "
    "solo 'template' (o inspect=true) para ver los diseños y sus marcadores; luego con 'slides': cada lámina elige "
    "un 'layout' (nombre o índice) y trae title, subtitle, bullets (texto o {text, level}), text, image_path, "
    "table {headers, rows}, notes y, si hace falta, fields {marcador: texto}. Las láminas de ejemplo de la plantilla "
    "se descartan (keep_template_slides=true las conserva). Los marcadores vacíos se eliminan para que no quede "
    "'Haga clic para agregar texto'. Si la plantilla trae láminas ya diseñadas (portadas, agendas, bloques con "
    "gráficos corporativos) que hay que duplicar en vez de armar desde un diseño, la inspección lista las formas de "
    "cada lámina (nombre, texto actual, posición, tamaño) y la lámina se pide con 'base' (número de lámina de la "
    "plantilla) y 'replace' {nombre de la forma o texto actual: texto nuevo}, más 'boxes' [{x, y, w, h en pulgadas, "
    "text|bullets, size, bold, color, font}] para cuerpos de texto nuevos; los logos, ondas e imágenes de la lámina "
    "base se conservan. Revisa 'avisos' en la respuesta. NO armes presentaciones con python-pptx y "
    "execute_code ni las revises lámina por lámina con vision_analyze salvo que el usuario lo pida."
)


def _load_edit_module():
    import importlib.util

    ruta = Path(__file__).with_name("editar.py")
    spec = importlib.util.spec_from_file_location("hq_pptx_editar", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _reservar_version(origen: Path) -> Path:
    for n in range(2, 1000):
        candidato = origen.with_name(f"{origen.stem}_v{n}{origen.suffix}")
        try:
            os.close(os.open(candidato, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return candidato
        except FileExistsError:
            continue
    raise ValueError("Hay demasiadas versiones de este archivo.")


_CARPETAS_DE_ARCHIVOS = ("outputs", "output", "uploads", "work", "sharepoint_downloads")


def _resolver_por_nombre(origen: Path) -> Path:
    if origen.exists() or origen.parent != _espacio():
        return origen
    base = _espacio()
    candidatos = []
    for carpeta in _CARPETAS_DE_ARCHIVOS:
        raiz = base / carpeta
        if raiz.is_dir():
            candidatos += [p for p in raiz.rglob(origen.name) if p.is_file() and ".hermes" not in p.parts]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        lista = ", ".join(str(c.relative_to(base)) for c in sorted(candidatos)[:6])
        raise ValueError(f"Hay varios archivos llamados {origen.name}: {lista}. Pasa la ruta completa de uno de ellos.")
    return origen


def _cambios_pedidos(args):
    cambios = args.get("cambios")
    if cambios in (None, "", []):
        cambios = args.get("operaciones")
    if isinstance(cambios, str):
        try:
            cambios = json.loads(cambios)
        except ValueError:
            return None
    if isinstance(cambios, dict):
        cambios = [cambios]
    return cambios


_PIDE_QUITAR = re.compile(r"elimin|quit[ae]|borr[ae]|remov|suprim|saca[rl]?\b|descart|deshaz", re.IGNORECASE)


def _mensaje_del_usuario() -> str:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(meta.get("mensaje_usuario") or payload.get("mensaje") or "")


def _borra_laminas_sin_que_se_pida(args) -> bool:
    cambios = _cambios_pedidos(args)
    if not isinstance(cambios, list):
        return False
    borra = any(isinstance(c, dict) and ("eliminar_lamina" in c or c.get("op") == "eliminar_lamina") for c in cambios)
    if not borra:
        return False
    mensaje = _mensaje_del_usuario()
    return bool(mensaje) and not _PIDE_QUITAR.search(mensaje)


def _handle_edit_pptx(args, **_kwargs):
    raw = str(args.get("path") or "").strip()
    if not raw:
        return json.dumps({"success": False, "error": "Falta 'path' (el .pptx a editar)."})
    if _borra_laminas_sin_que_se_pida(args):
        return json.dumps(
            {
                "success": False,
                "error": "El usuario no pidió eliminar ninguna lámina y esta edición eliminaría una. No se cambió "
                "nada. para «agrega una lámina» usa "
                "{agregar_lamina: {titulo, puntos}} de esta misma herramienta, sin borrar otra lámina.",
                "original_intacto": True,
            },
            ensure_ascii=False,
        )
    try:
        editor = _load_edit_module()
        origen = _resolver_por_nombre(_resolve_local_path(raw))
        if origen.suffix.lower() != ".pptx":
            return json.dumps({"success": False, "error": f"{origen.name} no es un .pptx."})
        editor.comprobar_archivo(origen)
        destino = _reservar_version(origen)
        resultado: dict = {}

        def _escribir(temporal):
            resultado.update(editor.editar(origen, temporal, _cambios_pedidos(args)))

        _guardar_seguro(destino, _escribir, _verificar_pptx)
    except (ValueError, OSError) as exc:
        return json.dumps({"success": False, "error": str(exc), "original_intacto": True}, ensure_ascii=False)
    relativo = str(destino.relative_to(_espacio())) if _espacio() in destino.parents else str(destino)
    return json.dumps(
        {
            "success": True,
            "path": relativo,
            "original": raw,
            "original_intacto": True,
            "comparacion": resultado,
            "siguiente": f"Entrega el archivo con la línea MEDIA:{relativo} y resume solo los cambios de 'comparacion'.",
        },
        ensure_ascii=False,
    )


def _linea_media(ruta: Path) -> list[str]:
    base = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd())).resolve()
    try:
        return [f"MEDIA:{ruta.resolve().relative_to(base)}"]
    except ValueError:
        return [f"MEDIA:{ruta}"]


def register(ctx):
    ctx.register_tool(
        name="hq_pptx_editar",
        toolset="hermeshq_pptx",
        schema={
            "name": "hq_pptx_editar",
            "description": (
                "Edita una presentación .pptx EXISTENTE (también la que subió el usuario) sin reconstruirla: "
                "reemplaza textos que ya existen, cambia títulos o notas, elimina láminas o agrega UNA TARJETA a una fila de "
                "tarjetas (agregar_tarjeta: clona la última y reparte el ancho; úsala para «agrega un cuarto riesgo»); "
                "o agrega UNA VIÑETA al final de la lista de una lámina (agregar_punto; úsala cuando la lámina de riesgos o "
                "pasos es una lista y no tarjetas). agrega UNA LÁMINA nueva al final (agregar_lamina: {titulo, puntos:[...], despues?}; úsala para «agrega una lámina de cierre»; NO la rehagas con python-pptx). NO agrega columnas ni formas: si piden eso, no lo hagas "
                "pisando otro texto; dilo y pregunta cómo seguir. Conserva todo lo demás (láminas, "
                "imágenes, tablas, gráficos y formato) y lo verifica comparando antes y después. Crea una versión "
                "nueva (_v2, _v3...) y nunca toca el original. Todo o nada: si un cambio no encuentra su texto, no "
                "se escribe nada. Usa esto en vez de python-pptx con execute_code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta del .pptx en tu espacio de trabajo."},
                    "cambios": {
                        "type": "array",
                        "description": (
                            "Una operación por elemento: {reemplazar:{texto, por, lamina?, ocurrencias?}} | "
                            "{titulo:{lamina, nuevo}} | {notas:{lamina, texto}} | {eliminar_lamina: n} | "
                            "{agregar_tarjeta:{lamina|lamina_titulo, titulo, puntos:[...]}} | "
                            "{agregar_punto:{lamina|lamina_titulo, texto}} | {agregar_lamina:{titulo, puntos:[...], despues?}}. "
                            "lamina empieza en 1."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["path", "cambios"],
            },
        },
        handler=_handle_edit_pptx,
        check_fn=_check_requirements,
        description="Edición segura de un .pptx existente, con versión nueva y verificación antes/después.",
        emoji="🛠️",
    )
    ctx.register_tool(
        name="hq_pptx_from_template",
        toolset="hermeshq_pptx",
        schema={
            "name": "hq_pptx_from_template",
            "description": DESC_FROM_TEMPLATE,
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "Ruta de la plantilla .pptx en el workspace."},
                    "inspect": {"type": "boolean", "description": "true: solo muestra diseños y marcadores."},
                    "slides": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Láminas en orden: [{layout, title, subtitle, bullets, text, image_path, table, notes, fields}] "
                            "o, para duplicar una lámina de la plantilla: [{base, replace, boxes, notes}]."
                        ),
                    },
                    "filename": {"type": "string", "description": "Nombre del archivo de salida."},
                    "keep_template_slides": {
                        "type": "boolean",
                        "description": "true: conserva las láminas que ya trae la plantilla y agrega las nuevas al final.",
                    },
                },
                "required": ["template"],
            },
        },
        handler=_handle_from_template,
        check_fn=_check_requirements,
        description="Generar una presentación con los diseños de una plantilla .pptx propia",
        emoji="🎨",
    )
    spec = {
        "name": "hq_pptx_generate",
        "description": (
            "Generate a PowerPoint (.pptx) presentation with a title slide plus content slides. "
            "Each content slide has a title, optional bullet points, an optional embedded image, and optional "
            "speaker notes. The file is saved to the agent's output directory and can be attached to messages. "
            "ALWAYS use this tool for PowerPoint files instead of python-pptx scripts with execute_code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Presentation title, shown on the title slide and used for the default filename",
                },
                "subtitle": {
                    "type": "string",
                    "description": "Optional subtitle shown on the title slide",
                },
                "slides": {
                    "type": "array",
                    "description": "Ordered list of content slides (the title slide is added automatically before these)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Slide title"},
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Bullet points for the slide body",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Optional speaker notes for this slide",
                            },
                            "image_path": {
                                "type": "string",
                                "description": (
                                    "Path to an existing local image file (png/jpg/jpeg/gif/bmp), relative to the "
                                    "workspace or absolute. The file must already exist (e.g. uploaded by the user "
                                    "or produced by another tool); this does not fetch URLs. If the slide also has "
                                    "bullets, the image is placed beside them; otherwise it's centered below the title."
                                ),
                            },
                            "image_width_inches": {
                                "type": "number",
                                "description": "Optional image display width in inches. If omitted, a sensible default is used based on the layout.",
                            },
                            "layout": {
                                "type": "string",
                                "enum": ["content", "section"],
                                "description": "'content' (default) for a normal title+bullets slide, 'section' for a section-divider slide",
                            },
                        },
                        "required": ["title"],
                    },
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename (e.g. 'propuesta.pptx'). If omitted, derived from title.",
                },
            },
            "required": ["title", "slides"],
        },
        "handler": _handle_generate_pptx,
        "emoji": "📊",
    }

    ctx.register_tool(
        name=spec["name"],
        toolset="hermeshq_pptx",
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
