from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socket
import urllib.request
from datetime import datetime
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CSS = """
@page {
    size: A4;
    margin: 2cm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-size: 9px;
        color: #999;
    }
}
body {
    font-family: 'Helvetica', 'Arial', sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #333;
}
h1 {
    font-size: 22pt;
    color: #1a1a2e;
    border-bottom: 3px solid #0f3460;
    padding-bottom: 8px;
    margin-bottom: 16px;
}
h2 {
    font-size: 16pt;
    color: #16213e;
    margin-top: 24px;
}
h3 {
    font-size: 13pt;
    color: #0f3460;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background-color: #0f3460;
    color: white;
    font-weight: bold;
}
tr:nth-child(even) {
    background-color: #f8f9fa;
}
blockquote {
    border-left: 4px solid #0f3460;
    margin: 12px 0;
    padding: 8px 16px;
    background-color: #f0f4ff;
    color: #555;
}
code {
    background-color: #f4f4f4;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
    font-size: 10pt;
}
.footer {
    margin-top: 32px;
    padding-top: 12px;
    border-top: 1px solid #ddd;
    font-size: 9pt;
    color: #999;
    text-align: center;
}
"""


BASE_CSS = """
img {
    display: block;
    max-width: 100%;
    max-height: 14cm;
    width: auto;
    height: auto;
    margin: 12px auto;
    page-break-inside: avoid;
}
"""

_H1_INICIAL = re.compile(r"^\s*<h1\b[^>]*>(.*?)</h1>", re.I | re.S)


def _sin_etiquetas(texto: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html_unescape(texto)).split()).casefold()


def _check_requirements():
    try:
        import weasyprint  # noqa: F401

        return True
    except ImportError:
        return False


def _sanitize_html(html: str) -> str:
    try:
        import nh3

        return nh3.clean(
            html,
            tags={
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "p",
                "br",
                "hr",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "ul",
                "ol",
                "li",
                "blockquote",
                "code",
                "pre",
                "img",
                "strong",
                "em",
                "b",
                "i",
                "u",
                "span",
                "div",
                "a",
            },
            attributes={
                "img": {"src", "alt", "width", "height"},
                "a": {"href"},
                "td": {"colspan", "rowspan"},
                "th": {"colspan", "rowspan"},
            },
            url_schemes={"data", "https"},
        )
    except ImportError:
        return html


MAX_BYTES_RECURSO = 5 * 1024 * 1024


def _ip_publica(hostname: str) -> bool:
    import ipaddress

    try:
        direcciones = {info[4][0] for info in socket.getaddrinfo(hostname, 443)}
    except (socket.gaierror, UnicodeError):
        return False
    if not direcciones:
        return False
    for direccion in direcciones:
        ip = ipaddress.ip_address(direccion.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def _is_private_ip(hostname: str) -> bool:
    return not _ip_publica(hostname)


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _como_fetcher(funcion):
    try:
        from weasyprint.urls import URLFetcher, URLFetcherResponse
    except ImportError:
        return funcion

    class _Fetcher(URLFetcher):
        def fetch(self, url, headers=None):
            if urlparse(url).scheme == "data":
                return super().fetch(url, headers)
            recurso = funcion(url)
            return URLFetcherResponse(
                recurso.get("redirected_url") or url,
                body=recurso.get("string") or b"",
                headers={"Content-Type": recurso.get("mime_type") or "application/octet-stream"},
            )

    return _Fetcher()


def _safe_url_fetcher(url: str, timeout: int = 10, ssl_context=None):
    parsed = urlparse(url)
    vacio = {"string": b"", "mime_type": "text/plain"}
    if parsed.scheme == "data":
        try:
            from weasyprint import default_url_fetcher
        except ImportError:
            return vacio
        return default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)
    if parsed.scheme != "https" or not parsed.hostname or not _ip_publica(parsed.hostname):
        return vacio
    abridor = urllib.request.build_opener(_SinRedirecciones())
    try:
        with abridor.open(urllib.request.Request(url, headers={"User-Agent": "HermesHQ-PDF"}), timeout=timeout) as r:
            if r.status != 200:
                return vacio
            datos = r.read(MAX_BYTES_RECURSO + 1)
            if len(datos) > MAX_BYTES_RECURSO:
                return vacio
            return {"string": datos, "mime_type": r.headers.get_content_type(), "redirected_url": url}
    except Exception:  # noqa: BLE001
        return vacio


_IMG_SRC = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(.*?)\2", re.I | re.S)
MAX_BYTES_IMAGEN_LOCAL = 8 * 1024 * 1024


def _incrustar_imagenes_locales(html: str, carpeta: Path) -> tuple[str, list[str], list[str]]:
    no_incluidas: list[str] = []
    remotas: list[str] = []

    def reemplazar(m):
        src = html_unescape(m.group(3)).strip()
        if src.lower().startswith("data:"):
            return m.group(0)
        if src.lower().startswith(("https://", "http://")):
            remotas.append(src)
            return m.group(0)
        ruta = src[7:] if src.lower().startswith("file://") else src
        candidato = (Path(ruta) if ruta.startswith("/") else carpeta / ruta).resolve()
        tipo = mimetypes.guess_type(candidato.name)[0] or ""
        try:
            candidato.relative_to(carpeta)
            valido = (
                candidato.is_file() and tipo.startswith("image/") and candidato.stat().st_size <= MAX_BYTES_IMAGEN_LOCAL
            )
        except (ValueError, OSError):
            valido = False
        if not valido:
            no_incluidas.append(src)
            return m.group(0)
        datos = base64.b64encode(candidato.read_bytes()).decode("ascii")
        return f"{m.group(1)}{m.group(2)}data:{tipo};base64,{datos}{m.group(2)}"

    return _IMG_SRC.sub(reemplazar, html), no_incluidas, remotas


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


def _verificar_pdf(ruta: Path) -> None:
    datos = ruta.read_bytes()
    if not datos.startswith(b"%PDF-") or b"%%EOF" not in datos[-2048:]:
        raise ValueError("el PDF generado está incompleto")


def _handle_generate_pdf(args, **_kwargs):
    title = args.get("title", "Documento")
    html_content = _sanitize_html(args.get("html_content", ""))
    css_content = args.get("css_content", "")
    filename = args.get("filename", "")

    try:
        from weasyprint import CSS, HTML
    except (ImportError, OSError):
        return json.dumps({"success": False, "error": "weasyprint is not installed. PDF generation is unavailable."})

    filename = _nombre_seguro(filename or title.lower(), "pdf", "documento")
    work_dir = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd())).resolve()
    html_content, no_incluidas, remotas = _incrustar_imagenes_locales(html_content, work_dir)
    if no_incluidas:
        return json.dumps(
            {
                "success": False,
                "error": "No se generó el PDF: estas imágenes no existen en tu espacio de trabajo o no son imágenes "
                f"válidas: {', '.join(no_incluidas)}. Usa la ruta que devolvió la herramienta (por ejemplo "
                "uploads/archivo.png) o quítalas.",
            },
            ensure_ascii=False,
        )
    fallidas: list[str] = []

    def _buscar(url, *a, **kw):
        recurso = _safe_url_fetcher(url, *a, **kw)
        if url in remotas and not recurso.get("string"):
            fallidas.append(url)
        return recurso

    output_dir = work_dir / "output"
    titulo_html = html_escape(str(title))
    inicial = _H1_INICIAL.match(html_content)
    repite_titulo = bool(inicial) and _sin_etiquetas(inicial.group(1)) == _sin_etiquetas(str(title))
    encabezado = "" if repite_titulo else f"<h1>{titulo_html}</h1>"

    full_html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>{titulo_html}</title>
</head>
<body>
    {encabezado}
    {html_content}
    <div class="footer">
        Generado el {datetime.now().strftime("%d/%m/%Y %H:%M")} por HermesHQ
    </div>
</body>
</html>"""

    combined_css = BASE_CSS + (css_content if css_content.strip() else DEFAULT_CSS)

    try:
        filepath = _reservar_salida(output_dir, filename)
        _guardar_seguro(
            filepath,
            lambda t: HTML(string=full_html, url_fetcher=_como_fetcher(_buscar)).write_pdf(
                str(t), stylesheets=[CSS(string=combined_css, url_fetcher=_como_fetcher(_safe_url_fetcher))]
            ),
            _verificar_pdf,
        )
    except Exception as exc:
        return json.dumps({"success": False, "error": f"PDF generation failed: {exc}"})
    if fallidas:
        filepath.unlink(missing_ok=True)
        return json.dumps(
            {
                "success": False,
                "error": "No se generó el PDF: no se pudieron descargar estas imágenes y el PDF saldría sin ellas: "
                f"{', '.join(fallidas)}. Descárgalas al espacio de trabajo y usa su ruta, o quítalas.",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "success": True,
            "filename": filepath.name,
            "path": str(filepath),
            "size_bytes": filepath.stat().st_size,
            "adjuntar_en_la_respuesta": _linea_media(filepath),
        }
    )


def _linea_media(ruta: Path) -> list[str]:
    base = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd())).resolve()
    try:
        return [f"MEDIA:{ruta.resolve().relative_to(base)}"]
    except ValueError:
        return [f"MEDIA:{ruta}"]


def register(ctx):
    specs = [
        {
            "name": "hq_pdf_generate",
            "description": (
                "Generate a professional PDF document from HTML content. "
                "Supports headings, paragraphs, tables, lists, images, blockquotes, and custom CSS styling. "
                "The PDF is saved to the agent's output directory and can be attached to messages. "
                "ALWAYS use this tool for PDFs instead of writing reportlab/weasyprint/fpdf scripts with execute_code: "
                "a multi-page report renders in under a second, so only the HTML body is needed (title and default "
                "styling are added for you)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Document title (shown as h1 heading and in metadata)",
                    },
                    "html_content": {
                        "type": "string",
                        "description": "HTML body content (without html/head/body tags). Supports h1-h6, p, table, ul, ol, blockquote, code, img, strong, em, etc.",
                    },
                    "css_content": {
                        "type": "string",
                        "description": "Optional custom CSS to override default styling. If omitted, professional default styling is applied.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional output filename (e.g. 'report.pdf'). If omitted, derived from title.",
                    },
                },
                "required": ["title", "html_content"],
            },
            "handler": _handle_generate_pdf,
            "emoji": "📄",
        },
    ]

    for spec in specs:
        ctx.register_tool(
            name=spec["name"],
            toolset="hermeshq_pdf",
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
