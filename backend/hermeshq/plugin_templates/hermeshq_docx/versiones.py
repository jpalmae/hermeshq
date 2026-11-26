from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EXTENSIONES = (".docx", ".pptx", ".xlsx")
CARPETAS = ("outputs", "output", "uploads", "work", "sharepoint_downloads")
_SUFIJO_VERSION = re.compile(r"(?:[_-]?(?:v\d+|editado|restaurada)(?:-\d+)?)+$", re.IGNORECASE)
_PREFIJO_ID = re.compile(r"^[0-9a-f]{8}_")
MAX_VERSIONES = 25
MAX_LINEAS_RESUMEN = 4


def base_de(nombre: str) -> str:
    raiz = _PREFIJO_ID.sub("", Path(nombre).stem)
    return (_SUFIJO_VERSION.sub("", raiz) or raiz).lower()


def encontrar(espacio: Path, nombre: str) -> list[Path]:
    objetivo = base_de(nombre)
    ext = Path(nombre).suffix.lower()
    hallados: list[Path] = []
    for carpeta in CARPETAS:
        raiz = espacio / carpeta
        if not raiz.is_dir():
            continue
        for ruta in raiz.rglob("*"):
            if (
                ruta.is_file()
                and ".hermes" not in ruta.parts
                and ruta.suffix.lower() in EXTENSIONES
                and (not ext or ruta.suffix.lower() == ext)
                and base_de(ruta.name) == objetivo
            ):
                hallados.append(ruta)
    return sorted(set(hallados), key=lambda r: (r.stat().st_mtime, r.name))[-MAX_VERSIONES:]


def _lineas(ruta: Path) -> list[str]:
    ext = ruta.suffix.lower()
    try:
        if ext == ".docx":
            from docx import Document

            doc = Document(str(ruta))
            lineas = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for tabla in doc.tables:
                lineas += [" | ".join(c.text.strip() for c in fila.cells) for fila in tabla.rows]
            return lineas
        if ext == ".pptx":
            from pptx import Presentation

            lineas = []
            for n, lamina in enumerate(Presentation(str(ruta)).slides, start=1):
                for forma in lamina.shapes:
                    if (
                        getattr(forma, "has_text_frame", False)
                        and forma.has_text_frame
                        and forma.text_frame.text.strip()
                    ):
                        lineas.append(f"[lámina {n}] " + forma.text_frame.text.strip().replace("\n", " / "))
            return lineas
        if ext == ".xlsx":
            from openpyxl import load_workbook

            lineas = []
            for ws in load_workbook(str(ruta), read_only=False).worksheets:
                for fila in ws.iter_rows(values_only=True):
                    valores = [str(v) for v in fila if v not in (None, "")]
                    if valores:
                        lineas.append(f"[{ws.title}] " + " | ".join(valores))
            return lineas
    except Exception:  # noqa: BLE001
        pass
    return []


def _ok_zip(ruta: Path) -> bool:
    try:
        return zipfile.is_zipfile(ruta)
    except OSError:
        return False


def listar(espacio: Path, nombre: str) -> list[dict]:
    versiones = [r for r in encontrar(espacio, nombre) if _ok_zip(r)]
    resultado, anterior = [], None
    santiago = ZoneInfo("America/Santiago")
    for numero, ruta in enumerate(versiones, start=1):
        actuales = _lineas(ruta)
        agregadas = removidas = []
        if anterior is not None:
            previas = set(anterior)
            agregadas = [x for x in actuales if x not in previas]
            removidas = [x for x in anterior if x not in set(actuales)]
        resultado.append(
            {
                "version": numero,
                "archivo": str(ruta.relative_to(espacio)),
                "fecha": datetime.fromtimestamp(ruta.stat().st_mtime, santiago).strftime("%d-%m-%Y %H:%M"),
                "tamano_bytes": ruta.stat().st_size,
                "lineas": len(actuales),
                "agrega": [x[:90] for x in agregadas[:MAX_LINEAS_RESUMEN]],
                "quita": [x[:90] for x in removidas[:MAX_LINEAS_RESUMEN]],
                "agrega_total": len(agregadas),
                "quita_total": len(removidas),
                "primera": anterior is None,
            }
        )
        anterior = actuales
    return resultado


def restaurar(espacio: Path, nombre: str, version: int) -> tuple[Path | None, str | None]:
    versiones = listar(espacio, nombre)
    elegida = next((v for v in versiones if v["version"] == version), None)
    if elegida is None:
        return None, f"No existe la versión {version}; hay {len(versiones)} (1 a {len(versiones)})."
    origen = espacio / elegida["archivo"]
    ext = origen.suffix
    carpeta = origen.parent
    base = base_de(origen.name)
    for n in range(1, 100):
        destino = carpeta / (f"{base}_restaurada{ext}" if n == 1 else f"{base}_restaurada-{n}{ext}")
        if not destino.exists():
            shutil.copyfile(origen, destino)
            return destino, None
    return None, "Hay demasiadas copias restauradas; limpia las antiguas."
