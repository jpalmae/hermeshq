from __future__ import annotations

import zipfile
from pathlib import Path

OPERACIONES = ("agregar_columna", "agregar_filas", "celda", "reemplazar", "eliminar_columna")
PARTES_NO_CONSERVADAS = (
    "xl/charts/",
    "xl/drawings/",
    "xl/pivotTables/",
    "xl/pivotCache/",
    "xl/externalLinks/",
    "xl/media/",
)


class EdicionRechazada(ValueError):
    pass


def comprobar_archivo(ruta: Path) -> None:
    if not ruta.is_file():
        raise EdicionRechazada(f"No existe el archivo {ruta.name}.")
    if not zipfile.is_zipfile(ruta):
        raise EdicionRechazada(f"{ruta.name} no es un .xlsx válido (está dañado, protegido o es otro formato).")
    with zipfile.ZipFile(ruta) as z:
        nombres = z.namelist()
    if "xl/workbook.xml" not in nombres:
        raise EdicionRechazada(f"{ruta.name} no es un libro de Excel (.xlsx).")
    perdidas = sorted({p.split("/")[1] for p in nombres if p.startswith(PARTES_NO_CONSERVADAS)})
    if perdidas:
        raise EdicionRechazada(
            f"{ruta.name} trae {', '.join(perdidas)} (gráficos, imágenes, tablas dinámicas o vínculos) que esta "
            "herramienta no puede conservar al guardar: no se editó nada. Pídele al usuario que lo edite en Excel o "
            "dile qué cambio exacto hacer a mano."
        )


def _hoja(libro, nombre, numero):
    if nombre is None or str(nombre).strip() == "":
        if len(libro.sheetnames) == 1:
            return libro[libro.sheetnames[0]]
        raise EdicionRechazada(f"Cambio {numero}: indica «hoja» (hay varias: {', '.join(libro.sheetnames)}).")
    for existente in libro.sheetnames:
        if existente.lower() == str(nombre).strip().lower():
            return libro[existente]
    raise EdicionRechazada(f"Cambio {numero}: no existe la hoja «{nombre}». Hojas: {', '.join(libro.sheetnames)}.")


def _fila_de_encabezados(ws) -> int:
    for fila in range(1, min(ws.max_row, 30) + 1):
        llenas = [c for c in ws[fila] if c.value not in (None, "")]
        if len(llenas) >= 2:
            return fila
    return 1


def _columna_por_nombre(ws, ref, encabezados: int, numero: int) -> int:
    from openpyxl.utils import column_index_from_string

    texto = str(ref).strip()
    for celda in ws[encabezados]:
        if celda.value is not None and str(celda.value).strip().lower() == texto.lower():
            return celda.column
    if texto.isalpha() and len(texto) <= 3:
        return column_index_from_string(texto.upper())
    raise EdicionRechazada(f"Cambio {numero}: no encuentro la columna «{texto}» en la fila {encabezados}.")


def _copiar_formato(origen, destino) -> None:
    if origen.has_style:
        destino._style = origen._style  # noqa: SLF001


def _ultima_fila_con_datos(ws, encabezados: int) -> int:
    ultima = encabezados
    for fila in range(encabezados + 1, ws.max_row + 1):
        if any(c.value not in (None, "") for c in ws[fila]):
            ultima = fila
    return ultima


def _agregar_columna(ws, datos: dict, numero: int) -> dict:
    encabezado = str(datos.get("encabezado") or "").strip()
    if not encabezado:
        raise EdicionRechazada(f"Cambio {numero}: agregar_columna necesita «encabezado».")
    fila_enc = _fila_de_encabezados(ws)
    ultima = _ultima_fila_con_datos(ws, fila_enc)
    ultima_col = max((c.column for c in ws[fila_enc] if c.value not in (None, "")), default=ws.max_column)
    despues = datos.get("despues_de")
    if despues is not None:
        indice = _columna_por_nombre(ws, despues, fila_enc, numero) + 1
        if indice <= ultima_col:
            if ws.merged_cells.ranges or ws.conditional_formatting or ws.data_validations.dataValidation:
                raise EdicionRechazada(
                    f"Cambio {numero}: la hoja «{ws.title}» tiene celdas combinadas, formato condicional o "
                    "validaciones: insertar una columna en el medio las desplazaría mal. Agrégala al final (sin "
                    "«despues_de») o indica la posición exacta al usuario."
                )
            for fila in ws.iter_rows():
                for celda in fila:
                    if isinstance(celda.value, str) and celda.value.startswith("="):
                        raise EdicionRechazada(
                            f"Cambio {numero}: la hoja «{ws.title}» tiene fórmulas que se romperían al insertar una "
                            "columna en el medio. Agrégala al final (sin «despues_de»)."
                        )
            ws.insert_cols(indice)
    else:
        indice = ultima_col + 1
    modelo = indice - 1 if indice - 1 >= 1 else indice
    ws.cell(row=fila_enc, column=indice, value=encabezado)
    _copiar_formato(ws.cell(row=fila_enc, column=modelo), ws.cell(row=fila_enc, column=indice))
    valores = datos.get("valores")
    for fila in range(fila_enc + 1, ultima + 1):
        celda = ws.cell(row=fila, column=indice)
        _copiar_formato(ws.cell(row=fila, column=modelo), celda)
        if isinstance(valores, list) and fila - fila_enc - 1 < len(valores):
            celda.value = valores[fila - fila_enc - 1]
        elif datos.get("valor") is not None:
            celda.value = datos["valor"]
    letra = ws.cell(row=fila_enc, column=indice).column_letter
    ancho = ws.column_dimensions[ws.cell(row=fila_enc, column=modelo).column_letter].width
    if ancho:
        ws.column_dimensions[letra].width = max(ancho, len(encabezado) + 2)
    return {"operacion": "agregar_columna", "hoja": ws.title, "columna": letra, "encabezado": encabezado}


def _agregar_filas(ws, datos: dict, numero: int) -> dict:
    filas = datos.get("filas")
    if not isinstance(filas, list) or not filas or not all(isinstance(f, list) for f in filas):
        raise EdicionRechazada(f"Cambio {numero}: agregar_filas necesita «filas» (lista de listas).")
    fila_enc = _fila_de_encabezados(ws)
    ultima = _ultima_fila_con_datos(ws, fila_enc)
    for k, valores in enumerate(filas, start=1):
        destino = ultima + k
        for col, valor in enumerate(valores, start=1):
            celda = ws.cell(row=destino, column=col, value=valor)
            _copiar_formato(ws.cell(row=max(ultima, fila_enc + 1), column=col), celda)
        if ws.row_dimensions[ultima].height:
            ws.row_dimensions[destino].height = ws.row_dimensions[ultima].height
    return {"operacion": "agregar_filas", "hoja": ws.title, "agregadas": len(filas), "desde_fila": ultima + 1}


def _celda(ws, datos: dict, numero: int) -> dict:
    ref = str(datos.get("ref") or "").strip().upper()
    if not ref or "valor" not in datos:
        raise EdicionRechazada(f"Cambio {numero}: «celda» necesita «ref» (por ejemplo B3) y «valor».")
    try:
        celda = ws[ref]
    except ValueError as exc:
        raise EdicionRechazada(f"Cambio {numero}: referencia de celda inválida «{ref}».") from exc
    if type(celda).__name__ == "MergedCell":
        raise EdicionRechazada(
            f"Cambio {numero}: {ref} es parte de una celda combinada; indica la celda superior izquierda."
        )
    anterior = celda.value
    celda.value = datos["valor"]
    return {"operacion": "celda", "hoja": ws.title, "ref": ref, "antes": anterior, "despues": datos["valor"]}


def _reemplazar(libro, datos: dict, numero: int) -> dict:
    buscar, por = str(datos.get("buscar") or ""), str(datos.get("por") if datos.get("por") is not None else "")
    if not buscar:
        raise EdicionRechazada(f"Cambio {numero}: «buscar» no puede estar vacío.")
    hojas = [_hoja(libro, datos["hoja"], numero)] if datos.get("hoja") else [libro[n] for n in libro.sheetnames]
    total = 0
    for ws in hojas:
        for fila in ws.iter_rows():
            for celda in fila:
                if isinstance(celda.value, str) and not celda.value.startswith("=") and buscar in celda.value:
                    celda.value = celda.value.replace(buscar, por)
                    total += 1
    if total == 0:
        raise EdicionRechazada(f"Cambio {numero}: no encontré «{buscar}». No se cambió nada.")
    return {"operacion": "reemplazar", "buscar": buscar, "por": por, "celdas": total}


def _eliminar_columna(ws, datos: dict, numero: int) -> dict:
    fila_enc = _fila_de_encabezados(ws)
    indice = _columna_por_nombre(ws, datos.get("encabezado") or datos.get("columna") or "", fila_enc, numero)
    if ws.merged_cells.ranges:
        raise EdicionRechazada(
            f"Cambio {numero}: la hoja «{ws.title}» tiene celdas combinadas; no se puede quitar una columna sin desordenarlas."
        )
    nombre = ws.cell(row=fila_enc, column=indice).value
    ws.delete_cols(indice)
    return {"operacion": "eliminar_columna", "hoja": ws.title, "encabezado": nombre}


def aplicar(libro, cambios: list) -> tuple[list[dict], dict]:
    if not isinstance(cambios, list) or not cambios:
        raise EdicionRechazada("Indica al menos un cambio en «cambios» (lista de operaciones).")
    detalle, tocado = [], {"hojas": set(), "columnas": set(), "filas": set(), "global": False}
    for numero, cambio in enumerate(cambios, start=1):
        if isinstance(cambio, dict) and "op" in cambio and len(cambio) > 1:
            cambio = {str(cambio["op"]): {k: v for k, v in cambio.items() if k != "op"}}
        if not isinstance(cambio, dict) or len(cambio) != 1 or next(iter(cambio)) not in OPERACIONES:
            raise EdicionRechazada(
                f"Cambio {numero}: usa una sola operación de {', '.join(OPERACIONES)}, por ejemplo "
                '{"agregar_columna": {"hoja": "Cálculo", "encabezado": "SUCURSAL"}}.'
            )
        operacion, datos = next(iter(cambio.items()))
        if not isinstance(datos, dict):
            raise EdicionRechazada(f"Cambio {numero}: faltan los datos de «{operacion}».")
        if operacion == "reemplazar":
            detalle.append(_reemplazar(libro, datos, numero))
            tocado["global"] = True
            continue
        ws = _hoja(libro, datos.get("hoja"), numero)
        tocado["hojas"].add(ws.title)
        if operacion == "agregar_columna":
            detalle.append(_agregar_columna(ws, datos, numero))
            tocado["global"] = True
        elif operacion == "eliminar_columna":
            detalle.append(_eliminar_columna(ws, datos, numero))
            tocado["global"] = True
        elif operacion == "agregar_filas":
            detalle.append(_agregar_filas(ws, datos, numero))
        else:
            detalle.append(_celda(ws, datos, numero))
            tocado["global"] = True
    return detalle, tocado


def _huella_estilo(celda) -> tuple:
    return (
        repr(celda.font),
        repr(celda.fill),
        repr(celda.border),
        repr(celda.alignment),
        celda.number_format,
    )


def _foto(libro) -> dict:
    foto = {}
    for ws in libro.worksheets:
        celdas = {}
        for fila in ws.iter_rows():
            for celda in fila:
                if celda.value is not None or celda.has_style:
                    celdas[(celda.row, celda.column)] = (celda.value, _huella_estilo(celda))
        foto[ws.title] = {
            "celdas": celdas,
            "combinadas": sorted(str(r) for r in ws.merged_cells.ranges),
            "anchos": {k: v.width for k, v in ws.column_dimensions.items() if v.width},
            "estado": ws.sheet_state,
        }
    return foto


def diferencias(antes: dict, despues: dict, detalle: list[dict]) -> list[str]:
    problemas = []
    if list(antes) != list(despues):
        problemas.append(f"Cambió la lista de hojas ({list(antes)} → {list(despues)}).")
        return problemas
    hojas_con_cambio = {d["hoja"] for d in detalle if "hoja" in d}
    hay_global = any(d["operacion"] == "reemplazar" for d in detalle)
    for nombre, previa in antes.items():
        nueva = despues[nombre]
        if previa["estado"] != nueva["estado"]:
            problemas.append(f"Hoja «{nombre}»: cambió su visibilidad.")
        if nombre not in hojas_con_cambio and not hay_global:
            if previa["celdas"] != nueva["celdas"] or previa["combinadas"] != nueva["combinadas"]:
                problemas.append(f"Hoja «{nombre}»: cambió sin pedirlo.")
    return problemas


def editar(origen: Path, destino_temporal: Path, cambios: list) -> dict:
    from openpyxl import load_workbook

    comprobar_archivo(origen)
    try:
        libro = load_workbook(str(origen))
        referencia = load_workbook(str(origen))
    except Exception as exc:  # noqa: BLE001
        raise EdicionRechazada(f"No pude abrir {origen.name} como libro de Excel: {exc}") from exc
    antes = _foto(referencia)
    detalle, _ = aplicar(libro, cambios)
    libro.save(str(destino_temporal))
    guardado = load_workbook(str(destino_temporal))
    problemas = diferencias(antes, _foto(guardado), detalle)
    if problemas:
        raise EdicionRechazada("La edición cambiaría cosas no pedidas: " + " ".join(problemas))
    return {
        "hojas": guardado.sheetnames,
        "cambios": detalle,
        "filas_por_hoja": {ws.title: ws.max_row for ws in guardado.worksheets},
    }
