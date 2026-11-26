from __future__ import annotations

import json
import os
import re
from pathlib import Path

_INVALID_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")
_SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _check_requirements():
    try:
        import openpyxl  # noqa: F401

        return True
    except ImportError:
        return False


def _safe_filename(title: str, ext: str) -> str:
    safe_title = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in title.lower())[:60].strip("-")
    return f"{safe_title or 'hoja-de-calculo'}.{ext}"


def _sanitize_sheet_name(name: str, index: int, taken: set[str]) -> str:
    cleaned = _INVALID_SHEET_CHARS.sub(" ", name or "").strip()[:31] or f"Hoja{index}"
    candidate = cleaned
    suffix = 2
    while candidate.lower() in taken:
        candidate = f"{cleaned[:28]} {suffix}"
        suffix += 1
    taken.add(candidate.lower())
    return candidate


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


def _verificar_xlsx(ruta: Path) -> None:
    from openpyxl import load_workbook

    _verificar_ooxml(ruta)
    load_workbook(str(ruta), read_only=True).close()


def _add_sheet_images(ws, images: list, ncols: int, get_column_letter) -> None:
    from openpyxl.drawing.image import Image as XLImage

    default_col = ncols + 2 if ncols else 2
    for idx, img_spec in enumerate(images):
        if not isinstance(img_spec, dict):
            continue
        raw_path = str(img_spec.get("path") or "").strip()
        if not raw_path:
            raise ValueError("image entry is missing 'path'")
        resolved = _resolve_local_path(raw_path)
        if not resolved.is_file():
            raise ValueError(f"image not found: {raw_path}")
        if resolved.suffix.lower() not in _SUPPORTED_IMAGE_EXTS:
            raise ValueError(
                f"unsupported image format '{resolved.suffix}' for {raw_path}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTS))}"
            )
        xl_image = XLImage(str(resolved))
        width_inches = img_spec.get("width_inches")
        try:
            width_inches = float(width_inches) if width_inches is not None else None
        except (TypeError, ValueError):
            width_inches = None
        if width_inches and xl_image.width:
            width_inches = max(0.5, min(width_inches, 10.0))
            scale = (width_inches * 96) / xl_image.width
            xl_image.width = int(xl_image.width * scale)
            xl_image.height = int(xl_image.height * scale)
        anchor = str(img_spec.get("anchor") or "").strip()
        if not anchor:
            anchor = f"{get_column_letter(default_col)}{1 + idx * 16}"
        ws.add_image(xl_image, anchor)


_PREFIJOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")


FUNCIONES_PERMITIDAS = frozenset(
    [
        "SUM",
        "SUMIF",
        "SUMIFS",
        "SUMPRODUCT",
        "AVERAGE",
        "AVERAGEIF",
        "AVERAGEIFS",
        "MAX",
        "MIN",
        "COUNT",
        "COUNTA",
        "COUNTIF",
        "COUNTIFS",
        "MEDIAN",
        "ROUND",
        "ROUNDUP",
        "ROUNDDOWN",
        "ABS",
        "IF",
        "IFERROR",
        "AND",
        "OR",
        "NOT",
        "VLOOKUP",
        "HLOOKUP",
        "INDEX",
        "MATCH",
        "TODAY",
        "CONCATENATE",
        "LEFT",
        "RIGHT",
        "MID",
        "LEN",
        "UPPER",
        "LOWER",
        "TRIM",
        "TEXT",
        "PRODUCT",
        "STDEV",
        "VAR",
        "LARGE",
        "SMALL",
        "RANK",
        "PERCENTILE",
    ]
)
_FUNCION = re.compile(r"([A-Za-z][A-Za-z0-9._]*)\s*\(")
_FORMULA_SEGURA = re.compile(r"^=[A-Za-z0-9_ ,.:;()!$'+\-*/<>=&%^\"]{1,254}$")


def _es_formula_segura(valor) -> bool:
    if not isinstance(valor, str) or not valor.startswith("="):
        return False
    if not _FORMULA_SEGURA.match(valor) or "[" in valor or "://" in valor.lower() or "\\" in valor:
        return False
    return all(f.upper() in FUNCIONES_PERMITIDAS for f in _FUNCION.findall(valor))


def _escribir_celda(ws, fila: int, columna: int, valor, formulas: bool = False):
    celda = ws.cell(row=fila, column=columna, value=valor)
    if isinstance(valor, str) and valor.startswith(_PREFIJOS_FORMULA):
        if formulas and _es_formula_segura(valor):
            celda.data_type = "f"
        else:
            celda.data_type = "s"
    return celda


_TIPOS_GRAFICO = ("line", "bar", "column", "pie")


def _columna_de(ref, headers: list[str]) -> int | None:
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if ref >= 1 else None
    texto = str(ref or "").strip()
    if texto.isdigit():
        return int(texto) or None
    for i, h in enumerate(headers, start=1):
        if h.strip().lower() == texto.lower():
            return i
    return None


def _es_numero(valor) -> bool:
    return isinstance(valor, int | float) and not isinstance(valor, bool)


_NUMERO_TEXTO = re.compile(r"^(0|[1-9]\d{0,14})(\.\d{1,10})?$")


def _numero_si_corresponde(valor):
    if isinstance(valor, str) and _NUMERO_TEXTO.match(valor.strip()):
        texto = valor.strip()
        return float(texto) if "." in texto else int(texto)
    return valor


_TOTAL = re.compile(r"^\s*(sub)?total\b", re.IGNORECASE)


_FILA_DE_TOTAL = re.compile(r"^\s*(total(es)?|subtotal|suma|promedio)\s*:?\s*$", re.I)


def _etiqueta(valor) -> str:
    return str(valor if valor is not None else "").strip()


def _filas_nombradas(nombres, rows: list, categoria: int) -> list[int] | None:
    etiquetas = [_etiqueta(r[categoria - 1]).lower() if categoria - 1 < len(r) else "" for r in rows]
    indices = []
    for nombre in nombres:
        clave = _etiqueta(nombre).lower()
        if not clave or clave not in etiquetas:
            return None
        indices.append(etiquetas.index(clave))
    return indices


def _series_por_filas(spec, n, headers, rows, ncols, categoria, primera):
    if not headers:
        raise ValueError(f"el gráfico {n} con series por filas necesita encabezados (serán el eje X)")
    columnas = [
        c
        for c in range(1, ncols + 1)
        if c != categoria
        and any(c - 1 < len(r) and _es_numero(r[c - 1]) for r in rows)
        and not _TOTAL.match(headers[c - 1] or "")
    ]
    if not columnas:
        raise ValueError(f"el gráfico {n} no tiene columnas numéricas para el eje X")
    if columnas != list(range(columnas[0], columnas[-1] + 1)):
        raise ValueError(f"las columnas numéricas del gráfico {n} no son contiguas; ordénalas juntas en la hoja")
    nombres = spec.get("series")
    if nombres:
        indices = _filas_nombradas(nombres, rows, categoria)
        if indices is None:
            raise ValueError(f"series del gráfico {n} que no existen como filas: {', '.join(map(str, nombres))}")
    else:
        indices = [
            i
            for i, r in enumerate(rows)
            if not _TOTAL.match(_etiqueta(r[categoria - 1]) if categoria - 1 < len(r) else "")
            and any(c - 1 < len(r) and _es_numero(r[c - 1]) for c in columnas)
        ]
    if not indices:
        raise ValueError(f"el gráfico {n} no tiene filas con datos para graficar")
    return columnas, indices


def _agregar_graficos(
    ws,
    graficos: list,
    headers: list[str],
    rows: list,
    ncols: int,
    get_column_letter,
    ws_datos=None,
    ncols_destino: int | None = None,
    desde: int = 0,
) -> list:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference, Series

    datos = ws_datos if ws_datos is not None else ws

    if not rows:
        raise ValueError("un gráfico necesita filas de datos")
    creados = []
    primera = 2 if headers else 1
    ultima = primera + len(rows) - 1
    for n, spec in enumerate(graficos, start=1):
        if not isinstance(spec, dict):
            raise ValueError(f"el gráfico {n} debe ser un objeto")
        tipo = str(spec.get("type") or "line").strip().lower()
        if tipo not in _TIPOS_GRAFICO:
            raise ValueError(f"tipo de gráfico no soportado: {tipo} (usa line, bar, column o pie)")
        categoria = _columna_de(spec.get("category_column", 1), headers)
        aviso_categoria = None
        if not categoria or categoria > ncols:
            if not any(isinstance(r[0] if r else None, str) for r in rows):
                raise ValueError(f"category_column del gráfico {n} no existe en la hoja")
            aviso_categoria = (
                f"category_column '{spec.get('category_column')}' no existe; se usaron las etiquetas de la columna 1"
                + (f" («{headers[0]}»)" if headers else "")
            )
            categoria = 1
        ultima_g = ultima
        while ultima_g > primera:
            etiqueta_final = (
                rows[ultima_g - primera][categoria - 1] if categoria - 1 < len(rows[ultima_g - primera]) else ""
            )
            if not _FILA_DE_TOTAL.match(str(etiqueta_final or "")):
                break
            ultima_g -= 1
        nombres = spec.get("series")
        por_filas = bool(spec.get("series_from_rows"))
        if (
            not por_filas
            and nombres
            and all(_columna_de(x, headers) is None for x in nombres)
            and _filas_nombradas(nombres, rows, categoria) is not None
        ):
            por_filas = True
        if tipo == "pie":
            grafico = PieChart()
        elif tipo == "line":
            grafico = LineChart()
        else:
            grafico = BarChart()
            grafico.type = "bar" if tipo == "bar" else "col"
        grafico.title = str(spec.get("title") or "")[:120] or None
        if tipo != "pie":
            grafico.y_axis.title = str(spec.get("y_title") or "")[:80] or None
            grafico.x_axis.title = str(spec.get("x_title") or "")[:80] or None
        if por_filas:
            columnas, indices = _series_por_filas(spec, n, headers, rows, ncols, categoria, primera)
            if tipo == "pie":
                indices = indices[:1]
            for i in indices:
                fila = primera + i
                valores = Reference(datos, min_col=columnas[0], max_col=columnas[-1], min_row=fila, max_row=fila)
                grafico.series.append(Series(valores, title=_etiqueta(rows[i][categoria - 1])[:80] or None))
            grafico.set_categories(
                Reference(datos, min_col=columnas[0], max_col=columnas[-1], min_row=primera - 1, max_row=primera - 1)
            )
            nombres_series = [_etiqueta(rows[i][categoria - 1]) for i in indices]
        else:
            if nombres:
                series = [_columna_de(x, headers) for x in nombres]
                faltan = [str(x) for x, c in zip(nombres, series) if not c or c > ncols]
                if faltan:
                    raise ValueError(f"series del gráfico {n} que no existen: {', '.join(faltan)}")
            else:
                series = [
                    c
                    for c in range(1, ncols + 1)
                    if c != categoria and any(c - 1 < len(r) and _es_numero(r[c - 1]) for r in rows)
                ]
            if not series:
                raise ValueError(f"el gráfico {n} no tiene columnas numéricas para graficar")
            if tipo == "pie":
                series = series[:1]
            for c in series:
                desde = primera - 1 if headers else primera
                grafico.add_data(
                    Reference(datos, min_col=c, max_col=c, min_row=desde, max_row=ultima_g),
                    titles_from_data=bool(headers),
                )
            grafico.set_categories(
                Reference(datos, min_col=categoria, max_col=categoria, min_row=primera, max_row=ultima_g)
            )
            nombres_series = [headers[c - 1] if headers else c for c in series]
        grafico.width, grafico.height = 18, 9
        ancla = (
            str(spec.get("anchor") or "").strip()
            or f"{get_column_letter((ncols_destino or ncols) + 2)}{2 + (desde + n - 1) * 20}"
        )
        ws.add_chart(grafico, ancla)
        creado = {"hoja": ws.title, "tipo": tipo, "series": nombres_series}
        if por_filas:
            creado["series_por"] = "filas"
        if aviso_categoria:
            creado["aviso"] = aviso_categoria
        creados.append(creado)
    return creados


_RANGO_SIN_HOJA = re.compile(
    r"(?<![!:A-Za-z0-9_$'])\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?(?![A-Za-z0-9_(])"
)


def _numero_de_columna(letras: str) -> int:
    n = 0
    for ch in letras:
        n = n * 26 + (ord(ch) - 64)
    return n


def _rechazar_formula_circular(hoja: str, fila: int, columna: int, valor) -> None:
    if not isinstance(valor, str) or not valor.startswith("="):
        return
    for m in _RANGO_SIN_HOJA.finditer(valor):
        c1, f1 = _numero_de_columna(m.group(1)), int(m.group(2))
        c2, f2 = (_numero_de_columna(m.group(3)), int(m.group(4))) if m.group(3) else (c1, f1)
        if min(c1, c2) <= columna <= max(c1, c2) and min(f1, f2) <= fila <= max(f1, f2):
            raise ValueError(
                f"la fórmula «{valor}» de la hoja «{hoja}» (fila {fila}, columna {columna}) se incluye a sí misma "
                "(referencia circular); si los datos están en otra hoja, escribe el nombre de la hoja, por ejemplo "
                "=SUM(Datos!B2:B5)"
            )


def _hojas_desde_archivo(ruta_texto: str) -> list:
    ruta = _resolve_local_path(ruta_texto)
    if not ruta.is_file():
        raise ValueError(f"No encuentro '{ruta_texto}' en tu espacio de trabajo.")
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"'{ruta_texto}' no es un JSON válido: {exc}") from exc
    hojas = datos.get("sheets") if isinstance(datos, dict) else datos
    if not isinstance(hojas, list) or not hojas:
        raise ValueError(f"'{ruta_texto}' debe tener una lista de hojas o {{\"sheets\": [...]}}.")
    return hojas


_PIDE_FILA_TOTAL = re.compile(r"\b(?:fila|l[ií]nea)\s+(?:de\s+)?total(?:es)?\b|\bfila\s+de\s+sumas?\b", re.I)
_ETIQUETA_TOTAL = re.compile(r"^\s*(total(es)?|suma)\s*:?\s*$", re.I)


def _mensaje_del_usuario() -> str:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(meta.get("pedido_del_usuario") or meta.get("mensaje_usuario") or payload.get("mensaje") or "")


def _error_de_hojas(sheets) -> str:
    for i, hoja in enumerate(sheets, start=1):
        if not isinstance(hoja, dict):
            return (
                f"La hoja {i} de 'sheets' no es un objeto {{name, headers, rows}} (llegó {type(hoja).__name__}): el JSON "
                "de las hojas está mal armado, por eso no se generó nada. Revisa que cada fila sea una lista "
                "cerrada con ] y vuelve a llamar; para muchas filas escribe work/<tema>/hojas.json y pasa sheets_file."
            )
        nombre = str(hoja.get("name") or f"Hoja{i}")
        filas = hoja.get("rows") or []
        if not isinstance(filas, list):
            return f"La hoja «{nombre}»: 'rows' debe ser una lista de filas (cada fila, una lista de valores)."
        for n, fila in enumerate(filas, start=1):
            if not isinstance(fila, list):
                return (
                    f"La hoja «{nombre}»: la fila {n} de 'rows' no es una lista de valores (llegó "
                    f"{type(fila).__name__}); no se generó nada para no entregar una hoja sin sus datos."
                )
    return ""


def _omite_la_fila_total(sheets) -> bool:
    if not _PIDE_FILA_TOTAL.search(_mensaje_del_usuario()):
        return False
    if not isinstance(sheets, list) or not sheets:
        return False
    for hoja in sheets:
        for fila in (hoja.get("rows") if isinstance(hoja, dict) else None) or []:
            if isinstance(fila, list) and fila and _ETIQUETA_TOTAL.match(str(fila[0] or "")):
                return False
    return True


_ADJUNTO_XLSX = re.compile(r"uploads/[^\s\]\)]+\.xls[xm]\b", re.IGNORECASE)
_PIDE_EDITAR = re.compile(
    r"\b(?:agreg|a[ñn]ad|inclu|quit|elimin|borr|cambi|modific|edit|corrig|actualiz|reemplaz|ajust|mant[eé]n|insert|renombr"
    r"|reorden|separ|junt|dividi)\w*",
    re.IGNORECASE,
)
_PIDE_ARCHIVO_NUEVO = re.compile(
    r"\b(?:nuev[oa]s?|otro archivo|otro libro|desde cero|consolid\w*|resum\w*|a partir de|con los datos de|"
    r"usando los datos|gr[aá]fic\w*)\b",
    re.IGNORECASE,
)
_YA_AVISADO = {"edicion_de_adjunto": False}


def _edicion_de_adjunto_con_generar() -> str | None:
    if _YA_AVISADO["edicion_de_adjunto"]:
        return None
    mensaje = _mensaje_del_usuario()
    adjuntos = _ADJUNTO_XLSX.findall(mensaje)
    if not adjuntos or not _PIDE_EDITAR.search(mensaje) or _PIDE_ARCHIVO_NUEVO.search(mensaje):
        return None
    _YA_AVISADO["edicion_de_adjunto"] = True
    return (
        f"El usuario adjuntó un Excel ({adjuntos[0]}) y pidió modificarlo: hq_xlsx_generate crea un libro NUEVO y pierde "
        "el formato, las fórmulas y las celdas combinadas del original. Usa hq_xlsx_editar sobre ese mismo archivo "
        "(agregar_columna con hoja, encabezado, despues_de y valores; también edita celdas, filas y hojas) y entrega "
        "el resultado. Si de verdad necesitas un libro nuevo, vuelve a llamar a hq_xlsx_generate y explícale al usuario por qué."
    )


def _handle_generate_xlsx(args, **_kwargs):
    aviso_edicion = _edicion_de_adjunto_con_generar()
    if aviso_edicion:
        return json.dumps({"success": False, "error": aviso_edicion}, ensure_ascii=False)
    title = str(args.get("title") or "Hoja de cálculo").strip()
    sheets = args.get("sheets")
    filename = str(args.get("filename") or "").strip()
    if args.get("sheets_file") and not sheets:
        try:
            sheets = _hojas_desde_archivo(str(args["sheets_file"]))
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return json.dumps(
            {"success": False, "error": "openpyxl is not installed. Excel spreadsheet generation is unavailable."}
        )

    if not isinstance(sheets, list) or not sheets:
        return json.dumps(
            {"success": False, "error": "sheets must be a non-empty list of sheet objects (or pass sheets_file)"}
        )

    error_spec = _error_de_hojas(sheets)
    if error_spec:
        return json.dumps({"success": False, "error": error_spec}, ensure_ascii=False)

    if _omite_la_fila_total(sheets):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "El usuario pidió una fila Total y ninguna hoja la trae. Agrégala como última fila de la hoja "
                    'con la etiqueta «Total» y fórmulas (por ejemplo ["Total", "=SUM(B2:B4)", "=SUM(C2:C4)"]); '
                    "el gráfico la excluye solo. No se generó nada."
                ),
            },
            ensure_ascii=False,
        )

    if not filename:
        filename = _safe_filename(title, "xlsx")
    elif not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"

    work_dir = Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd()))
    output_dir = work_dir / "output"
    filename = _nombre_seguro(filename, "xlsx", "hoja-de-calculo")

    try:
        wb = Workbook()
        wb.remove(wb.active)
        taken_names: set[str] = set()
        graficos_creados = []
        filas_por_hoja = {}
        hojas_info: dict = {}
        graficos_de_otra_hoja: list = []
        for i, sheet_spec in enumerate(sheets, start=1):
            if not isinstance(sheet_spec, dict):
                continue
            name = _sanitize_sheet_name(str(sheet_spec.get("name") or ""), i, taken_names)
            ws = wb.create_sheet(title=name)
            headers = [str(h) for h in (sheet_spec.get("headers") or [])]
            rows = [
                [_numero_si_corresponde(v) for v in r] for r in (sheet_spec.get("rows") or []) if isinstance(r, list)
            ]
            ncols = len(headers) if headers else (max((len(r) for r in rows), default=0))
            filas_por_hoja[name] = len(rows)
            hojas_info[name] = (ws, headers, rows, ncols)

            r = 1
            if headers:
                for c, h in enumerate(headers, start=1):
                    cell = _escribir_celda(ws, 1, c, h)
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill(start_color="0F3460", end_color="0F3460", fill_type="solid")
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                ws.freeze_panes = "A2"
                r = 2

            for row in rows:
                for c, value in enumerate(row, start=1):
                    if _es_formula_segura(value):
                        _rechazar_formula_circular(name, r, c, value)
                    _escribir_celda(ws, r, c, value, formulas=True)
                r += 1

            for c in range(1, ncols + 1):
                max_len = len(str(headers[c - 1])) if headers and c - 1 < len(headers) else 0
                for row in rows:
                    if c - 1 < len(row) and row[c - 1] is not None:
                        max_len = max(max_len, len(str(row[c - 1])))
                ws.column_dimensions[get_column_letter(c)].width = min(max(max_len + 2, 8), 50)

            images = sheet_spec.get("images") or []
            if images:
                _add_sheet_images(ws, images, ncols, get_column_letter)

            graficos = sheet_spec.get("charts") or []
            propios = [g for g in graficos if not (isinstance(g, dict) and g.get("data_sheet"))]
            if propios:
                graficos_creados += _agregar_graficos(ws, propios, headers, rows, ncols, get_column_letter)
            for g in graficos:
                if isinstance(g, dict) and g.get("data_sheet"):
                    graficos_de_otra_hoja.append((ws, g, ncols, len(propios) + len(graficos_de_otra_hoja)))

        for ws_destino, spec, ncols_destino, orden in graficos_de_otra_hoja:
            nombre_datos = str(spec.get("data_sheet")).strip()
            origen = next((v for k, v in hojas_info.items() if k.lower() == nombre_datos.lower()), None)
            if origen is None:
                raise ValueError(
                    f"data_sheet «{nombre_datos}» no existe; las hojas son: {', '.join(hojas_info) or 'ninguna'}"
                )
            ws_datos, headers_datos, rows_datos, ncols_datos = origen
            graficos_creados += _agregar_graficos(
                ws_destino,
                [spec],
                headers_datos,
                rows_datos,
                ncols_datos,
                get_column_letter,
                ws_datos=ws_datos,
                ncols_destino=ncols_destino,
                desde=orden,
            )

        if not wb.sheetnames:
            wb.create_sheet(title="Hoja1")
        filepath = _reservar_salida(output_dir, filename)
        _guardar_seguro(filepath, lambda t: wb.save(str(t)), _verificar_xlsx)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Excel spreadsheet generation failed: {exc}"})

    resultado = {
        "success": True,
        "filename": filepath.name,
        "path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "sheet_count": len(wb.sheetnames),
        "filas_por_hoja": filas_por_hoja,
        "graficos": graficos_creados,
    }
    vacias = [n for n, f in filas_por_hoja.items() if not f]
    if vacias:
        resultado["aviso"] = (
            f"Hojas sin filas de datos (solo encabezados): {', '.join(vacias)}. Si debían traer datos, vuelve a "
            "generar el archivo con las filas o con sheets_file; no digas que el Excel está completo."
        )
    return json.dumps(resultado, ensure_ascii=False)


def _cargar_editor():
    import importlib.util

    ruta = Path(__file__).resolve().parent / "editar_xlsx.py"
    spec = importlib.util.spec_from_file_location("hq_xlsx_editar_impl", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _resolver_xlsx(raw: str) -> Path:
    ruta = _resolve_local_path(raw)
    if ruta.exists():
        return ruta
    base = _espacio()
    candidatos = []
    for carpeta in ("outputs", "output", "uploads", "work", "sharepoint_downloads"):
        raiz = base / carpeta
        if raiz.is_dir():
            candidatos += [p for p in raiz.rglob(ruta.name) if p.is_file() and ".hermes" not in p.parts]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        lista = ", ".join(str(c.relative_to(base)) for c in sorted(candidatos)[:6])
        raise ValueError(f"Hay varios archivos llamados {ruta.name}: {lista}. Pasa la ruta completa de uno.")
    return ruta


def _cambios_xlsx(args):
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


def _handle_edit_xlsx(args, **_kwargs):
    raw = str(args.get("path") or "").strip()
    if not raw:
        return json.dumps({"success": False, "error": "Falta 'path' (el .xlsx a editar)."}, ensure_ascii=False)
    try:
        editor = _cargar_editor()
        origen = _resolver_xlsx(raw)
        if origen.suffix.lower() != ".xlsx":
            return json.dumps({"success": False, "error": f"{origen.name} no es un .xlsx."}, ensure_ascii=False)
        editor.comprobar_archivo(origen)
        destino = _reservar_salida(origen.parent, f"{origen.stem}_v2.xlsx")
        resultado: dict = {}

        def _escribir(temporal):
            resultado.update(editor.editar(origen, temporal, _cambios_xlsx(args)))

        _guardar_seguro(destino, _escribir, _verificar_xlsx)
    except (ValueError, OSError) as exc:
        if "destino" in locals():
            Path(destino).unlink(missing_ok=True)
        return json.dumps({"success": False, "error": str(exc), "original_intacto": True}, ensure_ascii=False)
    relativo = str(destino.relative_to(_espacio())) if _espacio() in destino.parents else str(destino)
    return json.dumps(
        {
            "success": True,
            "path": relativo,
            "original": str(origen.relative_to(_espacio())) if _espacio() in origen.parents else str(origen),
            "original_intacto": True,
            "comparacion": resultado,
            "siguiente": f"Entrega el archivo con la línea MEDIA:{relativo} y resume solo los cambios de 'comparacion'.",
        },
        ensure_ascii=False,
    )


def register(ctx):
    spec = {
        "name": "hq_xlsx_generate",
        "description": (
            "Generate an Excel (.xlsx) workbook with one or more sheets, each from an optional header row, "
            "data rows, optional embedded images and optional native charts (line, bar, column, pie). Headers are styled bold with a colored background and "
            "the header row is frozen. The file is saved to the agent's output directory and can be attached "
            "to messages. ALWAYS use this tool for Excel files instead of openpyxl scripts with execute_code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Workbook title, used for the default filename",
                },
                "sheets": {
                    "type": "array",
                    "description": "Ordered list of sheets to create",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Sheet/tab name (max 31 chars, invalid characters are stripped)",
                            },
                            "headers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional column headers for the first row",
                            },
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": ["string", "number", "boolean", "null"]},
                                },
                                "description": "Data rows, each a list of cell values (string or number). A string starting with = is a REAL Excel formula when it only uses basic functions (SUM, AVERAGE, MAX, MIN, COUNT, ROUND, IF, VLOOKUP…); to total data from another sheet write the sheet name (=SUM(Datos!B2:B5)): a formula that includes its own cell is rejected.",
                            },
                            "charts": {
                                "type": "array",
                                "description": (
                                    "Native Excel charts built from this sheet's data. Use this whenever the user "
                                    "asks for a chart/gráfico in the Excel; never say a chart was added if you did "
                                    "not pass it here."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["line", "bar", "column", "pie"]},
                                        "title": {"type": "string"},
                                        "data_sheet": {
                                            "type": "string",
                                            "description": (
                                                "Name of ANOTHER sheet whose headers and rows feed the chart (e.g. a "
                                                "summary sheet with a chart of the 'Datos' sheet). Default: this sheet."
                                            ),
                                        },
                                        "category_column": {
                                            "type": ["string", "integer"],
                                            "description": "Header name or 1-based column with the labels (default 1).",
                                        },
                                        "series": {
                                            "type": "array",
                                            "items": {"type": ["string", "integer"]},
                                            "description": (
                                                "Header names or 1-based columns to plot (default: all numeric). "
                                                "With series_from_rows, row labels instead."
                                            ),
                                        },
                                        "series_from_rows": {
                                            "type": "boolean",
                                            "description": (
                                                "true when each ROW is one line/bar (e.g. one row per branch or "
                                                "product and the months/periods are the column headers). The "
                                                "headers become the X axis and Total columns/rows are left out. "
                                                "Use it for 'evolution of each <row item>' charts."
                                            ),
                                        },
                                        "x_title": {"type": "string"},
                                        "y_title": {"type": "string"},
                                        "anchor": {"type": "string", "description": "Top-left cell, e.g. 'H2'."},
                                    },
                                },
                            },
                            "images": {
                                "type": "array",
                                "description": "Optional images to embed in this sheet",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {
                                            "type": "string",
                                            "description": (
                                                "Path to an existing local image file (png/jpg/jpeg/gif/bmp), "
                                                "relative to the workspace or absolute. The file must already "
                                                "exist (e.g. uploaded by the user or produced by another tool); "
                                                "this does not fetch URLs."
                                            ),
                                        },
                                        "anchor": {
                                            "type": "string",
                                            "description": "Optional top-left anchor cell (e.g. 'F2'). If omitted, placed to the right of the data.",
                                        },
                                        "width_inches": {
                                            "type": "number",
                                            "description": "Optional display width in inches (aspect ratio is preserved). If omitted, the image's native size is used.",
                                        },
                                    },
                                    "required": ["path"],
                                },
                            },
                        },
                        "required": ["rows"],
                    },
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename (e.g. 'ventas.xlsx'). If omitted, derived from title.",
                },
                "sheets_file": {
                    "type": "string",
                    "description": (
                        "Instead of 'sheets': path (inside the workspace, e.g. work/tema/hojas.json) to a JSON file "
                        "with the same list of sheet objects. Use it when an execute_code script already built "
                        "the rows (dozens or hundreds): write the JSON there and pass its path, all sheets in ONE call."
                    ),
                },
            },
            "required": ["title"],
        },
        "handler": _handle_generate_xlsx,
        "emoji": "📈",
    }

    ctx.register_tool(
        name=spec["name"],
        toolset="hermeshq_xlsx",
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
        name="hq_xlsx_editar",
        toolset="hermeshq_xlsx",
        schema={
            "name": "hq_xlsx_editar",
            "description": (
                "Edita un Excel (.xlsx) EXISTENTE (también el que subió el usuario) CONSERVANDO su formato: agrega "
                "columnas y filas copiando el estilo de las vecinas, cambia celdas, reemplaza textos o quita una "
                "columna, y lo verifica comparando antes y después. Crea una versión nueva (_v2…) y nunca toca el "
                "original. ÚSALA siempre que el usuario envíe un Excel y pida agregar o cambiar algo («mantén el "
                "formato», «agrégale las columnas…»): NO regeneres el libro desde cero con hq_xlsx_generate. No "
                "conserva gráficos ni imágenes: si el libro los trae, lo dice y no edita."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta o nombre del .xlsx en tu espacio de trabajo."},
                    "cambios": {
                        "type": "array",
                        "description": (
                            "Una operación por elemento: {agregar_columna:{hoja, encabezado, despues_de?, valores?, "
                            "valor?}} | {agregar_filas:{hoja, filas:[[...]]}} | {celda:{hoja, ref:'B3', valor}} | "
                            "{reemplazar:{buscar, por, hoja?}} | {eliminar_columna:{hoja, encabezado}}."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["path", "cambios"],
            },
        },
        handler=_handle_edit_xlsx,
        check_fn=_check_requirements,
        description="Edición segura de un .xlsx existente, con versión nueva y verificación antes/después.",
        emoji="🛠️",
    )
