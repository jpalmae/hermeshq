#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import uuid
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
NS = {"w": W, "w14": W14, "w15": W15}


def _q(tag: str) -> str:
    pfx, local = tag.split(":")
    return f"{{{NS[pfx]}}}{local}"


MAX_MIEMBROS = 5000
MAX_BYTES_DESCOMPRIMIDOS = 300 * 1024 * 1024
MAX_BYTES_PARTE_XML = 64 * 1024 * 1024
OPERACIONES = (
    "eliminar_color",
    "quitar_comentarios",
    "listas_a_parrafos",
    "buscar_reemplazar",
    "agregar_parrafo",
    "eliminar_parrafo",
    "eliminar_tabla",
    "cambiar_celda",
)


class DocumentoRechazado(ValueError):
    pass


def _parser():
    return etree.XMLParser(
        remove_blank_text=False, resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )


def _parsear(nombre: str, datos: bytes):
    if len(datos) > MAX_BYTES_PARTE_XML:
        raise DocumentoRechazado(f"parte_demasiado_grande:{nombre}")
    try:
        arbol = etree.fromstring(datos, _parser())
    except etree.XMLSyntaxError as exc:
        raise DocumentoRechazado(f"xml_invalido:{nombre}") from exc
    if arbol.getroottree().docinfo.doctype:
        raise DocumentoRechazado(f"xml_con_dtd:{nombre}")
    return arbol


def _leer_paquete(archivo: str) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    try:
        z = zipfile.ZipFile(archivo)
    except zipfile.BadZipFile as exc:
        raise DocumentoRechazado("no_es_un_docx") from exc
    with z:
        infos = z.infolist()
        if len(infos) > MAX_MIEMBROS:
            raise DocumentoRechazado("demasiadas_partes")
        nombres = [i.filename for i in infos]
        if len(set(nombres)) != len(nombres):
            raise DocumentoRechazado("partes_duplicadas")
        crudo: dict[str, bytes] = {}
        total = 0
        for info in infos:
            with z.open(info) as f:
                datos = f.read(MAX_BYTES_DESCOMPRIMIDOS - total + 1)
            total += len(datos)
            if total > MAX_BYTES_DESCOMPRIMIDOS:
                raise DocumentoRechazado("descomprimido_demasiado_grande")
            crudo[info.filename] = datos
    return infos, crudo


def _sha256_archivo(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _escribir_sin_sobrescribir(salida: str, infos: list[zipfile.ZipInfo], crudo: dict[str, bytes]) -> None:
    if os.path.lexists(salida):
        raise DocumentoRechazado("salida_existe")
    temporal = os.path.join(os.path.dirname(salida) or ".", f".{os.path.basename(salida)}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED) as z:
            for info in infos:
                nuevo = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                nuevo.compress_type = zipfile.ZIP_DEFLATED
                nuevo.external_attr = info.external_attr
                z.writestr(nuevo, crudo[info.filename])
        with zipfile.ZipFile(temporal) as z:
            if z.testzip() is not None:
                raise DocumentoRechazado("salida_corrupta")
            _parsear("word/document.xml", z.read("word/document.xml"))
        try:
            os.link(temporal, salida)
        except FileExistsError as exc:
            raise DocumentoRechazado("salida_existe") from exc
    finally:
        if os.path.exists(temporal):
            os.unlink(temporal)


def _wval(el, attr="val"):
    return el.get(_q(f"w:{attr}")) if el is not None else None


RECETA = {
    "operaciones": [
        {
            "op": "eliminar_color",
            "campos": {
                "colores": "lista de hex opcional (ej. ['EE0000']); por defecto detecta rojos",
                "eliminar_parrafo_vacio": "bool (por defecto true): borra el párrafo si queda sin texto",
            },
        },
        {"op": "quitar_comentarios", "campos": {"cuales": "'todos' (por defecto) o 'resueltos'"}},
        {"op": "listas_a_parrafos", "campos": {}},
        {
            "op": "buscar_reemplazar",
            "campos": {
                "buscar": "texto a buscar",
                "reemplazar": "texto nuevo",
                "max": "int opcional",
                "todas": "true solo si piden cambiar todas las coincidencias",
            },
        },
        {
            "op": "agregar_parrafo",
            "campos": {
                "texto": "texto del párrafo nuevo",
                "despues_de": "texto de un párrafo existente; por defecto al final del documento",
                "estilo": "id de estilo opcional (ej. 'Heading1'); por defecto el formato del párrafo de referencia",
            },
        },
        {
            "op": "eliminar_parrafo",
            "campos": {"contiene": "texto que identifica el párrafo", "max": "int opcional (por defecto 1)"},
        },
        {
            "op": "cambiar_celda",
            "campos": {
                "tabla": "int: la tabla N (o 'contiene' con un texto de la tabla; con una sola tabla se puede omitir)",
                "fila": "texto de la primera celda de la fila (ej. el nombre de la tarea)",
                "columna": "encabezado de la columna (ej. 'Estado') o su número",
                "valor": "texto nuevo de esa celda",
            },
        },
        {
            "op": "eliminar_tabla",
            "campos": {
                "numero": "int: la tabla N del documento (1 = la primera, en orden de aparición); o bien",
                "contiene": "texto que aparece en la tabla",
            },
        },
    ]
}


def _es_rojo(hex_val: str, colores) -> bool:
    if not hex_val or hex_val.lower() in ("auto", "ffffff"):
        return False
    h = hex_val.strip().lstrip("#").upper()
    if len(h) != 6:
        return False
    if colores:
        return h in {c.strip().lstrip("#").upper() for c in colores}
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return False
    return r >= 0x90 and g <= 0x60 and b <= 0x60


def _texto_de_parrafo(p) -> str:
    return "".join(t.text or "" for t in p.iter(_q("w:t")))


HIGHLIGHT_ROJO = {"red", "darkred", "darkRed"}


def _op_eliminar_color(doc, op):
    colores = op.get("colores")
    borrar_vacios = op.get("eliminar_parrafo_vacio", True)
    incluir_resaltado = op.get("incluir_resaltado", True)
    runs_quitados = 0
    parrafos_afectados = set()
    for r in list(doc.iter(_q("w:r"))):
        rpr = r.find(_q("w:rPr"))
        color = rpr.find(_q("w:color")) if rpr is not None else None
        highlight = rpr.find(_q("w:highlight")) if rpr is not None else None
        es_rojo = color is not None and _es_rojo(_wval(color), colores)
        es_resaltado = (
            incluir_resaltado
            and highlight is not None
            and str(_wval(highlight)).lower() in {h.lower() for h in HIGHLIGHT_ROJO}
        )
        if es_rojo or es_resaltado:
            p = r.getparent()
            while p is not None and p.tag != _q("w:p"):
                p = p.getparent()
            if p is not None:
                parrafos_afectados.add(p)
            r.getparent().remove(r)
            runs_quitados += 1
    parrafos_borrados = 0
    if borrar_vacios:
        for p in parrafos_afectados:
            if not _texto_de_parrafo(p).strip():
                parent = p.getparent()
                if parent is not None:
                    parent.remove(p)
                    parrafos_borrados += 1
    marcas_limpiadas = 0
    for ppr in list(doc.iter(_q("w:pPr"))):
        rpr = ppr.find(_q("w:rPr"))
        if rpr is None:
            continue
        color = rpr.find(_q("w:color"))
        highlight = rpr.find(_q("w:highlight"))
        if color is not None and _es_rojo(_wval(color), colores):
            rpr.remove(color)
            marcas_limpiadas += 1
        if (
            incluir_resaltado
            and highlight is not None
            and str(_wval(highlight)).lower() in {h.lower() for h in HIGHLIGHT_ROJO}
        ):
            rpr.remove(highlight)
            marcas_limpiadas += 1
    return {
        "runs_en_color_quitados": runs_quitados,
        "parrafos_vacios_borrados": parrafos_borrados,
        "marcas_de_parrafo_limpiadas": marcas_limpiadas,
    }


def _op_listas_a_parrafos(doc, op):
    n = 0
    for numpr in list(doc.iter(_q("w:numPr"))):
        numpr.getparent().remove(numpr)
        n += 1
    return {"items_de_lista_convertidos": n}


def _textos_propios(p) -> list:
    return [t for t in p.iter(_q("w:t")) if _parrafo_de(t) is p]


def _parrafo_de(el):
    actual = el.getparent()
    while actual is not None and actual.tag != _q("w:p"):
        actual = actual.getparent()
    return actual


def _preservar(t) -> None:
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _op_buscar_reemplazar(doc, op):
    buscar = op.get("buscar") or ""
    reemplazar = op.get("reemplazar") or ""
    tope = op.get("max")
    if not buscar:
        raise DocumentoRechazado("buscar_vacio")
    if tope is not None and (not isinstance(tope, int) or isinstance(tope, bool) or tope < 1):
        raise DocumentoRechazado("max_invalido")
    clave = op.get("parrafo_contiene")
    if clave is not None and (not isinstance(clave, str) or not clave.strip()):
        raise DocumentoRechazado("parrafo_contiene_invalido")

    def en_alcance(texto):
        return not clave or clave in texto

    if op.get("todas") is not True and not clave:
        existentes = sum(
            texto.count(buscar)
            for texto in (
                "".join(t.text or "" for t in ts) for ts in (_textos_propios(p) for p in doc.iter(_q("w:p"))) if ts
            )
            if en_alcance(texto)
        )
        if existentes > 1:
            raise DocumentoRechazado(
                f"el_texto_aparece_{existentes}_veces_y_max_no_elige_cual_cambiar_pon_en_parrafo_contiene_un_texto_que_"
                "distinga_el_parrafo_pedido_(por_ejemplo_el_nombre_de_la_persona)_o_todas_true_solo_si_el_usuario_pidio_"
                "cambiar_todas"
            )
    hechos = 0
    parrafos = 0
    tocados = []
    for p in list(doc.iter(_q("w:p"))):
        if tope is not None and hechos >= tope:
            break
        ts = _textos_propios(p)
        if not ts:
            continue
        inicios = []
        posicion = 0
        for t in ts:
            inicios.append(posicion)
            posicion += len(t.text or "")
        texto = "".join(t.text or "" for t in ts)
        if not en_alcance(texto):
            continue
        coincidencias = []
        desde = 0
        while True:
            if tope is not None and hechos + len(coincidencias) >= tope:
                break
            i = texto.find(buscar, desde)
            if i < 0:
                break
            coincidencias.append(i)
            desde = i + len(buscar)
        if not coincidencias:
            continue
        parrafos += 1
        tocados.append(p)
        hechos += len(coincidencias)
        largos = [len(t.text or "") for t in ts]
        for inicio in reversed(coincidencias):
            fin = inicio + len(buscar)
            a = next(k for k in range(len(ts)) if inicios[k] <= inicio < inicios[k] + largos[k])
            b = next(k for k in range(len(ts)) if inicios[k] < fin <= inicios[k] + largos[k])
            ta, tb = ts[a], ts[b]
            if a == b:
                actual = ta.text or ""
                ta.text = actual[: inicio - inicios[a]] + reemplazar + actual[fin - inicios[a] :]
                _preservar(ta)
                continue
            ta.text = (ta.text or "")[: inicio - inicios[a]] + reemplazar
            _preservar(ta)
            for k in range(a + 1, b):
                ts[k].text = ""
            tb.text = (tb.text or "")[fin - inicios[b] :]
            _preservar(tb)
    resultado = {"reemplazos": hechos, "parrafos": parrafos}
    if tocados:
        resultado["textos_resultantes"] = [_texto_de_parrafo(p)[:90] for p in tocados[:3]]
    if hechos and buscar in reemplazar and buscar != reemplazar:
        resultado["ojo"] = (
            "«reemplazar» contiene a «buscar»: se reemplaza solo ese fragmento y lo demás del párrafo queda. Si querías "
            "cambiar el texto completo (p. ej. un título), pon el texto completo en «buscar». Revisa textos_resultantes."
        )
    return resultado


_ESTILO_DE_TITULO = re.compile(r"^(heading|title|subtitle|t[ií]tulo|ttulo|subt)", re.IGNORECASE)


def _p_en_cuerpo(doc):
    cuerpo = doc.find(_q("w:body"))
    if cuerpo is None:
        raise DocumentoRechazado("el .docx no tiene cuerpo")
    return cuerpo


def _parrafo_con(doc, texto: str):
    buscado = _compacto(texto)
    for p in doc.iter(_q("w:p")):
        if buscado and buscado in _compacto(_texto_de_parrafo(p)):
            return p
    return None


def _compacto(texto: str) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


def _solo_campos(op, permitidos):
    sobran = sorted(k for k in op if k != "op" and k not in permitidos)
    if sobran:
        raise DocumentoRechazado(
            f"campos_desconocidos en {op.get('op')}: {', '.join(sobran)} (validos: {', '.join(permitidos)}). "
            "Para una seccion con titulo usa dos operaciones: agregar_parrafo con estilo 'Heading1' y el titulo, y "
            "otra agregar_parrafo con el cuerpo y despues_de igual al titulo"
        )


def _op_agregar_parrafo(doc, op):
    _solo_campos(op, ("texto", "despues_de", "estilo"))
    texto = str(op.get("texto") or "")
    if not texto.strip():
        raise DocumentoRechazado("texto_vacio")
    cuerpo = _p_en_cuerpo(doc)
    referencia = None
    despues_de = op.get("despues_de")
    if despues_de:
        referencia = _parrafo_con(doc, despues_de)
        if referencia is None:
            raise DocumentoRechazado(f"no_se_encontro_el_parrafo: {despues_de}")
    else:
        con_texto = [p for p in cuerpo.findall(_q("w:p")) if _texto_de_parrafo(p).strip()]
        referencia = con_texto[-1] if con_texto else None
    nuevo = etree.Element(_q("w:p"))
    ppr = referencia.find(_q("w:pPr")) if referencia is not None else None
    if ppr is not None:
        ppr = copy.deepcopy(ppr)
        for numpr in ppr.findall(_q("w:numPr")):
            ppr.remove(numpr)
        estilo_actual = ppr.find(_q("w:pStyle"))
        if estilo_actual is not None and _ESTILO_DE_TITULO.match(estilo_actual.get(_q("w:val")) or ""):
            ppr.remove(estilo_actual)
        nuevo.append(ppr)
    estilo = op.get("estilo")
    if estilo:
        ppr = nuevo.find(_q("w:pPr"))
        if ppr is None:
            ppr = etree.SubElement(nuevo, _q("w:pPr"))
            nuevo.insert(0, ppr)
        pstyle = ppr.find(_q("w:pStyle"))
        if pstyle is None:
            pstyle = etree.Element(_q("w:pStyle"))
            ppr.insert(0, pstyle)
        pstyle.set(_q("w:val"), str(estilo))
    run = etree.SubElement(nuevo, _q("w:r"))
    rpr = None
    if referencia is not None and not estilo:
        primero = next((r for r in referencia.iter(_q("w:r")) if r.find(_q("w:rPr")) is not None), None)
        rpr = primero.find(_q("w:rPr")) if primero is not None else None
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t = etree.SubElement(run, _q("w:t"))
    t.text = texto
    _preservar(t)
    if referencia is not None and despues_de:
        referencia.addnext(nuevo)
    else:
        sect = cuerpo.find(_q("w:sectPr"))
        if sect is not None:
            sect.addprevious(nuevo)
        else:
            cuerpo.append(nuevo)
    return {"parrafos_agregados": 1}


def _op_eliminar_parrafo(doc, op):
    _solo_campos(op, ("contiene", "max"))
    contiene = op.get("contiene")
    if not contiene or not _compacto(contiene):
        raise DocumentoRechazado("contiene_vacio")
    tope = op.get("max", 1)
    if not isinstance(tope, int) or isinstance(tope, bool) or tope < 1:
        raise DocumentoRechazado("max_invalido")
    buscado = _compacto(contiene)
    candidatos = []
    for p in doc.iter(_q("w:p")):
        padre = p.getparent()
        if padre is None or (padre.tag == _q("w:tc") and len(padre.findall(_q("w:p"))) == 1):
            continue
        compacto = _compacto(_texto_de_parrafo(p))
        if buscado in compacto:
            candidatos.append((compacto == buscado, p))
    exactos = [p for exacto, p in candidatos if exacto]
    elegidos = exactos if exactos else [p for _, p in candidatos]
    if tope == 1 and not exactos and len(elegidos) > 1:
        lista = "; ".join(f"«{_texto_de_parrafo(p)[:50]}»" for p in elegidos[:4])
        raise DocumentoRechazado(
            f"«{contiene}» aparece en {len(elegidos)} párrafos ({lista}). Usa el texto completo del párrafo que "
            "quieres quitar."
        )
    eliminados = 0
    textos = []
    for p in elegidos[:tope]:
        textos.append(_texto_de_parrafo(p)[:60])
        p.getparent().remove(p)
        eliminados += 1
    return {"parrafos_eliminados": eliminados, **({"eliminado": textos} if textos else {})}


def _op_eliminar_tabla(doc, op):
    _solo_campos(op, ("numero", "contiene"))
    cuerpo = doc.find(_q("w:body"))
    tablas = [t for t in cuerpo.findall(_q("w:tbl"))] if cuerpo is not None else []
    numero, contiene = op.get("numero"), op.get("contiene")
    if (numero is None) == (not contiene):
        raise DocumentoRechazado("indica_numero_o_contiene")
    if numero is not None:
        if not isinstance(numero, int) or isinstance(numero, bool) or numero < 1:
            raise DocumentoRechazado("numero_invalido")
        if numero > len(tablas):
            raise DocumentoRechazado(f"el_documento_tiene_{len(tablas)}_tablas_y_pediste_la_{numero}")
        elegida = tablas[numero - 1]
    else:
        buscado = _compacto(contiene)
        if not buscado:
            raise DocumentoRechazado("contiene_vacio")
        coinciden = [t for t in tablas if buscado in _compacto("".join(t.itertext()))]
        if len(coinciden) != 1:
            raise DocumentoRechazado(f"el_texto_aparece_en_{len(coinciden)}_tablas_usa_numero")
        elegida = coinciden[0]
    elegida.getparent().remove(elegida)
    return {"tablas_eliminadas": 1}


def _texto_de_celda(celda) -> str:
    return _compacto("".join(celda.itertext()))


def _op_cambiar_celda(doc, op):
    _solo_campos(op, ("tabla", "contiene", "fila", "columna", "valor"))
    cuerpo = doc.find(_q("w:body"))
    tablas = list(cuerpo.findall(_q("w:tbl"))) if cuerpo is not None else []
    numero, contiene = op.get("tabla"), op.get("contiene")
    if numero is not None:
        if not isinstance(numero, int) or isinstance(numero, bool) or not 1 <= numero <= len(tablas):
            raise DocumentoRechazado(f"el_documento_tiene_{len(tablas)}_tablas_y_pediste_la_{numero}")
        elegida = tablas[numero - 1]
    elif contiene:
        coinciden = [t for t in tablas if _compacto(contiene) in _compacto("".join(t.itertext()))]
        if len(coinciden) != 1:
            raise DocumentoRechazado(f"el_texto_aparece_en_{len(coinciden)}_tablas_usa_tabla")
        elegida = coinciden[0]
    elif len(tablas) == 1:
        elegida = tablas[0]
    else:
        raise DocumentoRechazado(f"el_documento_tiene_{len(tablas)}_tablas_indica_tabla_o_contiene")
    if "valor" not in op:
        raise DocumentoRechazado("falta_valor")
    filas = elegida.findall(_q("w:tr"))
    if len(filas) < 2:
        raise DocumentoRechazado("la_tabla_no_tiene_filas_de_datos")
    encabezados = [_texto_de_celda(c) for c in filas[0].findall(_q("w:tc"))]
    columna = op.get("columna")
    if isinstance(columna, int) and not isinstance(columna, bool):
        if not 1 <= columna <= len(encabezados):
            raise DocumentoRechazado(f"la_tabla_tiene_{len(encabezados)}_columnas")
        indice = columna - 1
    else:
        buscada = _compacto(columna)
        posibles = [i for i, h in enumerate(encabezados) if h == buscada] or [
            i for i, h in enumerate(encabezados) if buscada and buscada in h
        ]
        if len(posibles) != 1:
            raise DocumentoRechazado(f"columna_no_encontrada_o_ambigua: encabezados: {', '.join(encabezados)}")
        indice = posibles[0]
    fila_buscada = _compacto(op.get("fila"))
    if not fila_buscada:
        raise DocumentoRechazado("falta_fila")
    coinciden = [
        f for f in filas[1:] if f.findall(_q("w:tc")) and _texto_de_celda(f.findall(_q("w:tc"))[0]) == fila_buscada
    ] or [f for f in filas[1:] if f.findall(_q("w:tc")) and fila_buscada in _texto_de_celda(f.findall(_q("w:tc"))[0])]
    if len(coinciden) != 1:
        raise DocumentoRechazado(f"la_fila_coincide_con_{len(coinciden)}_filas")
    celdas = coinciden[0].findall(_q("w:tc"))
    if indice >= len(celdas):
        raise DocumentoRechazado("la_fila_no_tiene_esa_columna")
    textos = list(celdas[indice].iter(_q("w:t")))
    anterior = "".join(t.text or "" for t in textos)
    nuevo = str(op["valor"])
    if textos:
        textos[0].text = nuevo
        textos[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for t in textos[1:]:
            t.text = ""
    else:
        parrafo = celdas[indice].find(_q("w:p"))
        if parrafo is None:
            parrafo = etree.SubElement(celdas[indice], _q("w:p"))
        corrida = etree.SubElement(parrafo, _q("w:r"))
        etree.SubElement(corrida, _q("w:t")).text = nuevo
    return {"celdas_cambiadas": 1, "antes": anterior, "despues": nuevo}


def _ids_resueltos(trees):
    ext = trees.get("word/commentsExtended.xml")
    com = trees.get("word/comments.xml")
    if ext is None or com is None:
        return set()
    done_paraids = set()
    for ce in ext.iter(_q("w15:commentEx")):
        if ce.get(_q("w15:done")) in ("1", "true"):
            pid = ce.get(_q("w15:paraId"))
            if pid:
                done_paraids.add(pid)
    ids = set()
    for c in com.iter(_q("w:comment")):
        for p in c.iter(_q("w:p")):
            pid = p.get(_q("w14:paraId"))
            if pid and pid in done_paraids:
                ids.add(_wval(c, "id"))
            break
    return {i for i in ids if i}


def _op_quitar_comentarios(doc, trees, op):
    cuales = (op.get("cuales") or "todos").lower()
    com = trees.get("word/comments.xml")
    if cuales == "resueltos":
        objetivo = _ids_resueltos(trees)
        if not objetivo:
            return {"comentarios_quitados": 0, "nota": "no hay comentarios marcados como resueltos"}
    else:
        objetivo = None

    def afecta(cid):
        return objetivo is None or cid in objetivo

    quitados = 0
    for tag in ("w:commentRangeStart", "w:commentRangeEnd"):
        for el in list(doc.iter(_q(tag))):
            if afecta(_wval(el, "id")):
                el.getparent().remove(el)
    for ref in list(doc.iter(_q("w:commentReference"))):
        if afecta(_wval(ref, "id")):
            run = ref.getparent()
            while run is not None and run.tag != _q("w:r"):
                run = run.getparent()
            if run is not None and run.getparent() is not None:
                run.getparent().remove(run)
            elif ref.getparent() is not None:
                ref.getparent().remove(ref)
    if com is not None:
        for c in list(com.iter(_q("w:comment"))):
            if afecta(_wval(c, "id")):
                c.getparent().remove(c)
                quitados += 1
    return {"comentarios_quitados": quitados}


def _fallar(error: str) -> None:
    print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
    sys.exit(1)


def _efecto(resultado: dict) -> int:
    return sum(v for k, v in resultado.items() if isinstance(v, int) and not isinstance(v, bool))


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--info":
        print(json.dumps({"ok": True, "receta": RECETA}, ensure_ascii=False))
        return
    if len(sys.argv) < 2:
        _fallar("falta entrada.json")
    with open(sys.argv[1], encoding="utf-8") as f:
        entrada = json.load(f)
    archivo = entrada.get("archivo")
    operaciones = entrada.get("operaciones") or []
    salida = entrada.get("salida")
    if not archivo or not os.path.isfile(archivo):
        _fallar(f"no existe el archivo: {archivo}")
    if not operaciones or not isinstance(operaciones, list):
        _fallar("sin operaciones")
    desconocidas = [
        op.get("op") if isinstance(op, dict) else op
        for op in operaciones
        if not isinstance(op, dict) or op.get("op") not in OPERACIONES
    ]
    if desconocidas:
        _fallar(f"operacion_desconocida: {desconocidas} (validas: {', '.join(OPERACIONES)})")
    if not salida:
        base, ext = os.path.splitext(archivo)
        salida = f"{base}_editado{ext}"
    if os.path.realpath(salida) == os.path.realpath(archivo):
        _fallar("la salida no puede ser el original")

    huella_origen = _sha256_archivo(archivo)
    esperada = entrada.get("huella_origen")
    if esperada and esperada != huella_origen:
        print(
            json.dumps({"ok": False, "error": "documento_cambio", "huella_actual": huella_origen}, ensure_ascii=False)
        )
        sys.exit(1)

    try:
        infos, crudo = _leer_paquete(archivo)
        partes_xml = ("word/document.xml", "word/comments.xml", "word/commentsExtended.xml")
        trees = {n: _parsear(n, crudo[n]) for n in partes_xml if n in crudo}
        doc = trees.get("word/document.xml")
        if doc is None:
            raise DocumentoRechazado("el .docx no tiene word/document.xml")

        resumen = {}
        avisos = []
        for indice, op in enumerate(operaciones):
            nombre = op["op"]
            if nombre == "eliminar_color":
                resultado = _op_eliminar_color(doc, op)
            elif nombre == "listas_a_parrafos":
                resultado = _op_listas_a_parrafos(doc, op)
            elif nombre == "buscar_reemplazar":
                resultado = _op_buscar_reemplazar(doc, op)
            elif nombre == "agregar_parrafo":
                resultado = _op_agregar_parrafo(doc, op)
            elif nombre == "eliminar_parrafo":
                resultado = _op_eliminar_parrafo(doc, op)
            elif nombre == "eliminar_tabla":
                resultado = _op_eliminar_tabla(doc, op)
            elif nombre == "cambiar_celda":
                resultado = _op_cambiar_celda(doc, op)
            else:
                resultado = _op_quitar_comentarios(doc, trees, op)
            resumen.setdefault(nombre, []).append(resultado)
            if _efecto(resultado) == 0:
                avisos.append(f"la operacion {indice + 1} ({nombre}) no encontro nada que cambiar")

        for n, tree in trees.items():
            crudo[n] = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
        _escribir_sin_sobrescribir(salida, infos, crudo)
    except DocumentoRechazado as exc:
        _fallar(str(exc))

    print(
        json.dumps(
            {
                "ok": True,
                "salida": salida,
                "resumen": resumen,
                "avisos": avisos,
                "sin_cambios": len(avisos) == len(operaciones),
                "huella_origen": huella_origen,
                "huella_salida": _sha256_archivo(salida),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
