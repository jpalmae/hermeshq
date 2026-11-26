from __future__ import annotations

import io
import os
import re
from html.parser import HTMLParser
from pathlib import Path

TEXTO = (".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".log", ".yaml", ".yml")
MAX_FILAS_HOJA = 2000


class _TextoVisible(HTMLParser):
    _OCULTOS = {"script", "style", "head", "noscript", "svg"}
    _BLOQUES = {"p", "div", "section", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._oculto = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._OCULTOS:
            self._oculto += 1
        elif tag in self._BLOQUES:
            self.partes.append("\n")
            if tag in ("h1", "h2", "h3", "h4"):
                self.partes.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self._OCULTOS:
            self._oculto = max(0, self._oculto - 1)
        elif tag in ("td", "th"):
            self.partes.append(" | ")
        elif tag in self._BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._oculto and data.strip():
            self.partes.append(" ".join(data.split()))


def _html_a_texto(datos: bytes) -> str:
    lector = _TextoVisible()
    lector.feed(datos.decode("utf-8", errors="replace"))
    texto = "".join(lector.partes)
    return re.sub(r"\n\s*\n+", "\n", texto).strip()


def _docx(datos: bytes) -> str:
    from docx import Document

    documento = Document(io.BytesIO(datos))
    partes = [p.text for p in documento.paragraphs if p.text.strip()]
    for tabla in documento.tables:
        for fila in tabla.rows:
            partes.append(" | ".join(celda.text.strip() for celda in fila.cells))
    return "\n".join(partes)


def _xlsx(datos: bytes) -> str:
    from openpyxl import load_workbook

    libro = load_workbook(io.BytesIO(datos), read_only=True, data_only=True)
    partes: list[str] = []
    for hoja in libro.worksheets:
        partes.append(f"## Hoja: {hoja.title}")
        for indice, fila in enumerate(hoja.iter_rows(values_only=True)):
            if indice >= MAX_FILAS_HOJA:
                partes.append(f"(hoja cortada en {MAX_FILAS_HOJA} filas)")
                break
            linea = " | ".join("" if v is None else str(v) for v in fila).rstrip(" |")
            if linea:
                partes.append(linea)
    return "\n".join(partes)


def _pptx(datos: bytes) -> str:
    from pptx import Presentation

    presentacion = Presentation(io.BytesIO(datos))
    partes: list[str] = []
    for numero, lamina in enumerate(presentacion.slides, start=1):
        partes.append(f"## Lámina {numero}")
        for forma in lamina.shapes:
            if getattr(forma, "has_text_frame", False) and forma.text_frame.text.strip():
                partes.append(forma.text_frame.text)
            if getattr(forma, "has_table", False) and forma.has_table:
                for fila in forma.table.rows:
                    partes.append(" | ".join(celda.text.strip() for celda in fila.cells))
        if lamina.has_notes_slide and lamina.notes_slide.notes_text_frame.text.strip():
            partes.append(f"(notas) {lamina.notes_slide.notes_text_frame.text}")
    return "\n".join(partes)


def _pdf(datos: bytes, desde: int, hasta: int | None) -> tuple[str, int]:
    from pypdf import PdfReader

    lector = PdfReader(io.BytesIO(datos))
    total = len(lector.pages)
    fin = min(total, hasta or total)
    partes = []
    for numero in range(max(1, desde), fin + 1):
        texto = (lector.pages[numero - 1].extract_text() or "").strip()
        if texto:
            partes.append(f"--- Página {numero} ---\n{texto}")
    return "\n".join(partes), total


PAGINAS_POR_IMAGEN = 6


def paginas_como_imagenes(ruta: Path, desde: int, hasta: int | None, destino: Path) -> list[str]:
    import fitz

    documento = fitz.open(str(ruta))
    try:
        fin = min(len(documento), hasta or len(documento), max(1, desde) + PAGINAS_POR_IMAGEN - 1)
        destino.mkdir(parents=True, exist_ok=True)
        rutas = []
        for numero in range(max(1, desde), fin + 1):
            imagen = destino / f"pagina-{numero:03d}.png"
            documento[numero - 1].get_pixmap(dpi=150).save(str(imagen))
            rutas.append(str(imagen))
        return rutas
    finally:
        documento.close()


def _escaneo(ruta: Path, desde: int, hasta: int | None, paginas: int) -> dict:
    base = {"success": False, "escaneado": True, "paginas": paginas}
    try:
        base_trabajo = Path(os.environ.get("HERMES_SESSION_CWD") or Path.cwd())
        imagenes = paginas_como_imagenes(ruta, desde, hasta, base_trabajo / "work" / "escaneos" / ruta.stem)
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "error": f"El PDF es un escaneo sin texto y no pude convertir sus páginas en imágenes ({exc}). Díselo al "
            "usuario y pídele una versión con texto. No uses otro archivo como fuente sin decírselo.",
        }
    ultima = desde + len(imagenes) - 1
    return {
        **base,
        "imagenes": imagenes,
        "error": "El PDF es un escaneo sin texto: convertí sus páginas en imágenes.",
        "siguiente": (
            "Lee cada imagen con vision_analyze (una por llamada), pidiendo la transcripción literal del texto. "
            "Dile al usuario que el documento es un escaneo y que el texto sale de una lectura por imagen, que puede "
            "tener errores. No uses otro archivo como fuente sin decírselo."
            + (f" Quedan páginas: sigue con desde_pagina={ultima + 1}." if ultima < (hasta or paginas) else "")
        ),
    }


def leer(ruta: Path, *, max_caracteres: int, desde_pagina: int = 1, hasta_pagina: int | None = None) -> dict:
    extension = ruta.suffix.lower()
    datos = ruta.read_bytes()
    extra: dict = {}
    if extension in (".html", ".htm"):
        tipo, texto = "html", _html_a_texto(datos)
    elif extension in TEXTO:
        tipo, texto = "texto", datos.decode("utf-8", errors="replace")
    elif extension == ".pdf":
        tipo = "pdf"
        texto, paginas = _pdf(datos, desde_pagina, hasta_pagina)
        extra["paginas"] = paginas
        if not texto.strip():
            return _escaneo(ruta, desde_pagina, hasta_pagina, paginas)
    elif extension == ".docx":
        tipo, texto = "word", _docx(datos)
    elif extension in (".xlsx", ".xlsm"):
        tipo, texto = "excel", _xlsx(datos)
    elif extension == ".pptx":
        tipo, texto = "powerpoint", _pptx(datos)
    else:
        return {"success": False, "error": f"Formato {extension or 'desconocido'} no soportado para lectura."}
    truncado = len(texto) > max_caracteres
    resultado = {
        "success": True,
        "nombre": ruta.name,
        "tipo": tipo,
        **extra,
        "texto": texto[:max_caracteres],
        "caracteres": len(texto),
        "truncado": truncado,
    }
    if truncado:
        resultado["siguiente"] = (
            "El texto se cortó. Para un PDF largo lee el resto por partes con desde_pagina/hasta_pagina. "
            "No uses execute_code para leerlo."
        )
    return resultado
