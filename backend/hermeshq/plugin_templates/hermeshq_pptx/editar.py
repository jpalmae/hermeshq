from __future__ import annotations

import copy
import hashlib
from pathlib import Path

FIRMA_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OPERACIONES = ("reemplazar", "titulo", "notas", "eliminar_lamina", "agregar_tarjeta", "agregar_punto", "agregar_lamina")


class EdicionRechazada(ValueError):
    pass


def comprobar_archivo(ruta: Path) -> None:
    import zipfile

    if not ruta.is_file():
        raise EdicionRechazada(f"No existe el archivo {ruta.name}.")
    with ruta.open("rb") as archivo:
        cabecera = archivo.read(8)
    if cabecera == FIRMA_CFB:
        raise EdicionRechazada(f"{ruta.name} está protegido con contraseña: pide al usuario una copia sin protección.")
    if not zipfile.is_zipfile(ruta):
        raise EdicionRechazada(f"{ruta.name} no es un .pptx válido (está dañado o es otro formato).")


TIPO_GRUPO, TIPO_IMAGEN = 6, 13
TIPO_AUTOFORMA = 1
ETIQUETA_FACTOR_MAXIMO = 3
ETIQUETA_AUMENTO_MAXIMO = 30


def _tipo_forma(forma):
    try:
        return forma.shape_type
    except Exception:  # noqa: BLE001
        return None


def _formas(contenedor):
    for forma in contenedor:
        yield forma
        if _tipo_forma(forma) == TIPO_GRUPO:
            yield from _formas(forma.shapes)


def _tipo(forma) -> str:
    if getattr(forma, "has_table", False):
        return "tabla"
    if getattr(forma, "has_chart", False):
        return "grafico"
    tipo = _tipo_forma(forma)
    if tipo == TIPO_IMAGEN:
        return "imagen"
    if tipo == TIPO_GRUPO:
        return "grupo"
    return "forma"


def _texto_de(forma) -> str:
    if getattr(forma, "has_table", False) and forma.has_table:
        return "\n".join(celda.text for fila in forma.table.rows for celda in fila.cells)
    if getattr(forma, "has_text_frame", False) and forma.has_text_frame:
        return forma.text_frame.text
    return ""


def _titulo(lamina) -> str:
    try:
        return lamina.shapes.title.text if lamina.shapes.title is not None else ""
    except Exception:  # noqa: BLE001
        return ""


NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
CT_LAMINA = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
CT_GLOBALES = tuple(
    f"application/vnd.openxmlformats-officedocument.{t}+xml"
    for t in (
        "presentationml.slideLayout",
        "presentationml.slideMaster",
        "presentationml.notesMaster",
        "presentationml.handoutMaster",
        "theme",
    )
)
RT_NOTAS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
HIJOS_PROPIOS_DE_GRUPO = (f"{NS_P}nvGrpSpPr", f"{NS_P}grpSpPr")


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _c14n(elemento) -> bytes:
    from lxml import etree

    return etree.tostring(elemento, method="c14n")


def _sin_hijos(elemento):
    copia = copy.deepcopy(elemento)
    for hijo in list(copia):
        if hijo.tag not in HIJOS_PROPIOS_DE_GRUPO:
            copia.remove(hijo)
    return copia


def _huella_forma(forma, tocada: bool, movida: bool = False, parrafos_libres: bool = False) -> str:
    elemento = forma._element  # noqa: SLF001
    elemento = _sin_hijos(elemento) if _tipo_forma(forma) == TIPO_GRUPO else copy.deepcopy(elemento)
    if movida:
        for tag in (f"{NS_A}off", f"{NS_A}ext"):
            for geometria in elemento.iter(tag):
                for atributo in list(geometria.attrib):
                    geometria.set(atributo, "0")
    if parrafos_libres:
        for cuerpo in elemento.iter(f"{NS_P}txBody"):
            for sobrante in cuerpo.findall(f"{NS_A}p")[1:]:
                cuerpo.remove(sobrante)
    if tocada:
        for texto in elemento.iter(f"{NS_A}t"):
            texto.text = ""
        for run in list(elemento.iter(f"{NS_A}r")):
            if run.find(f"{NS_A}rPr") is None:
                run.getparent().remove(run)
    return _sha(_c14n(elemento))


def _contenido_parte(parte) -> bytes:
    elemento = getattr(parte, "_element", None)
    return _c14n(elemento) if elemento is not None else bytes(parte.blob)


def _huella_parte(parte, ids_laminas: dict, visitadas: set) -> str:
    if parte.content_type == CT_LAMINA:
        return f"lamina:{ids_laminas.get(parte, 'huerfana')}"
    if parte.content_type in CT_GLOBALES:
        return f"global:{parte.partname}"
    if parte in visitadas:
        return f"ciclo:{parte.partname}"
    visitadas = visitadas | {parte}
    relaciones = sorted(
        (
            rid,
            rel.reltype,
            rel.target_ref if rel.is_external else _huella_parte(rel.target_part, ids_laminas, visitadas),
        )
        for rid, rel in parte.rels.items()
    )
    return _sha(_contenido_parte(parte) + repr(relaciones).encode())


def _relaciones_lamina(lamina, ids_laminas: dict) -> list:
    return sorted(
        (rid, rel.reltype, rel.target_ref if rel.is_external else _huella_parte(rel.target_part, ids_laminas, set()))
        for rid, rel in lamina.part.rels.items()
        if rel.reltype != RT_NOTAS
    )


def huella_global(presentacion) -> dict:
    elemento = copy.deepcopy(presentacion.part._element)  # noqa: SLF001
    for lista in elemento.findall(f"{NS_P}sldIdLst"):
        elemento.remove(lista)
    plantillas = {
        str(parte.partname): _sha(_contenido_parte(parte))
        for parte in presentacion.part.package.iter_parts()
        if parte.content_type in CT_GLOBALES
    }
    return {"presentacion": _sha(_c14n(elemento)), "plantillas": plantillas}


def foto(presentacion, tocadas: set | None = None) -> list[dict]:
    tocadas = tocadas or set()
    ids_laminas = {lamina.part: lamina.slide_id for lamina in presentacion.slides}
    resultado = []
    for lamina in presentacion.slides:
        formas = list(_formas(lamina.shapes))
        conteo: dict[str, int] = {}
        for forma in formas:
            conteo[_tipo(forma)] = conteo.get(_tipo(forma), 0) + 1
        arbol = lamina.shapes._spTree  # noqa: SLF001
        propia = _sin_hijos(arbol)
        lamina_sin_formas = copy.deepcopy(lamina._element)  # noqa: SLF001
        arbol_copia = lamina_sin_formas.find(f"{NS_P}cSld/{NS_P}spTree")
        if arbol_copia is not None:
            arbol_copia.getparent().replace(arbol_copia, propia)
        notas = lamina.notes_slide if lamina.has_notes_slide else None
        resultado.append(
            {
                "id": lamina.slide_id,
                "titulo": _titulo(lamina),
                "conteo": conteo,
                "textos": {forma.shape_id: _texto_de(forma) for forma in formas},
                "formas": {
                    forma.shape_id: _huella_forma(
                        forma,
                        (lamina.slide_id, forma.shape_id) in tocadas,
                        (lamina.slide_id, f"mov:{forma.shape_id}") in tocadas,
                        (lamina.slide_id, f"par:{forma.shape_id}") in tocadas,
                    )
                    for forma in formas
                },
                "orden": [forma.shape_id for forma in formas],
                "lamina": _sha(_c14n(lamina_sin_formas)),
                "relaciones": _relaciones_lamina(lamina, ids_laminas),
                "notas": notas.notes_text_frame.text if notas is not None else "",
                "notas_huella": _sha(_c14n(notas._element)) if notas is not None else "",  # noqa: SLF001
            }
        )
    return resultado


def _reemplazar_en_marco(marco, viejo: str, nuevo: str) -> tuple[int, bool]:
    hechas, simplificado = 0, False
    for parrafo in marco.paragraphs:
        en_runs = sum(run.text.count(viejo) for run in parrafo.runs)
        if en_runs:
            for run in parrafo.runs:
                run.text = run.text.replace(viejo, nuevo)
            hechas += en_runs
            continue
        completo = "".join(run.text for run in parrafo.runs)
        if parrafo.runs and viejo in completo:
            hechas += completo.count(viejo)
            parrafo.runs[0].text = completo.replace(viejo, nuevo)
            for run in parrafo.runs[1:]:
                run.text = ""
            simplificado = True
    return hechas, simplificado


def _laminas_objetivo(presentacion, numero):
    laminas = list(presentacion.slides)
    if numero is None:
        return list(enumerate(laminas, 1))
    try:
        indice = int(numero)
    except (TypeError, ValueError) as exc:
        raise EdicionRechazada(f"Lámina inválida: {numero!r}.") from exc
    if not 1 <= indice <= len(laminas):
        raise EdicionRechazada(f"La presentación tiene {len(laminas)} láminas; no existe la {indice}.")
    return [(indice, laminas[indice - 1])]


def _normalizar_cambio(cambio):
    if not isinstance(cambio, dict) or "op" not in cambio or len(cambio) < 2:
        return cambio
    resto = {k: v for k, v in cambio.items() if k != "op"}
    nombre = str(cambio["op"])
    if nombre in ("buscar_reemplazar", "reemplazar"):
        datos = {
            "texto": resto.pop("buscar", resto.pop("texto", "")),
            "por": resto.pop("reemplazar", resto.pop("por", "")),
        }
        for clave in ("lamina", "ocurrencias"):
            if clave in resto:
                datos[clave] = resto.pop(clave)
        for alias in ("max", "maximo", "max_ocurrencias", "count"):
            if alias in resto and "ocurrencias" not in datos:
                datos["ocurrencias"] = resto.pop(alias)
        if resto:
            raise EdicionRechazada(
                f"El cambio trae campos que no entiendo ({', '.join(sorted(resto))}) y los habría ignorado. Usa "
                "{reemplazar: {texto, por, lamina?, ocurrencias?}}. No se cambió nada."
            )
        return {"reemplazar": datos}
    if nombre in OPERACIONES:
        return {nombre: resto if nombre != "eliminar_lamina" else resto.get("lamina", resto.get("n"))}
    return cambio


def _no_encontrado(presentacion, numero: int, viejo: str, lamina) -> str:
    base = f"Cambio {numero}: no encontré «{viejo}»"
    if lamina is not None:
        base += f" en la lámina {lamina}"
    en, titulos = [], []
    for indice, lam in enumerate(presentacion.slides, start=1):
        titulos.append(f"{indice} «{_titulo(lam)}»")
        for forma in _formas(lam.shapes):
            marcos = []
            if getattr(forma, "has_table", False) and forma.has_table:
                marcos = [c.text_frame for f in forma.table.rows for c in f.cells]
            elif getattr(forma, "has_text_frame", False) and forma.has_text_frame:
                marcos = [forma.text_frame]
            if any(viejo.lower() in m.text.lower() for m in marcos):
                en.append(indice)
                break
    if en:
        return f"{base}. Ese texto está en la lámina {', '.join(map(str, en))}: repite el cambio con ese número. No se cambió nada."
    return (
        f"{base}. No aparece en ninguna lámina. Láminas del archivo: {'; '.join(titulos[:40])}. "
        "Usa el texto exacto de la lámina (o agregar_tarjeta si querías agregar una tarjeta). No se cambió nada."
    )


def aplicar(presentacion, cambios: list) -> tuple[list[dict], set, set]:
    if not isinstance(cambios, list) or not cambios:
        raise EdicionRechazada("Indica al menos un cambio en el parámetro «cambios» (una lista de operaciones).")
    cambios = [_normalizar_cambio(c) for c in cambios]
    detalle, tocadas, eliminadas = [], set(), set()
    for numero, cambio in enumerate(cambios, 1):
        if not isinstance(cambio, dict) or len(cambio) != 1 or next(iter(cambio)) not in OPERACIONES:
            raise EdicionRechazada(
                f"Cambio {numero}: usa una sola operación de {', '.join(OPERACIONES)}, por ejemplo "
                '{"reemplazar": {"texto": "…", "por": "…"}} o {"agregar_tarjeta": {"lamina": 5, "titulo": "…", '
                '"puntos": ["…"]}}.'
            )
        operacion, datos = next(iter(cambio.items()))
        if operacion == "eliminar_lamina":
            ((indice, lamina),) = _laminas_objetivo(presentacion, datos)
            eliminadas.add(lamina.slide_id)
            detalle.append({"operacion": operacion, "lamina": indice, "titulo": _titulo(lamina)})
            continue
        if not isinstance(datos, dict):
            raise EdicionRechazada(f"Cambio {numero}: faltan los datos de «{operacion}».")
        if operacion == "reemplazar":
            viejo, nuevo = str(datos.get("texto") or ""), str(datos.get("por") if datos.get("por") is not None else "")
            if not viejo:
                raise EdicionRechazada(f"Cambio {numero}: «texto» no puede estar vacío.")
            total, simplificados = 0, []
            for indice, lamina in _laminas_objetivo(presentacion, datos.get("lamina")):
                for forma in _formas(lamina.shapes):
                    marcos = []
                    if getattr(forma, "has_table", False) and forma.has_table:
                        marcos = [celda.text_frame for fila in forma.table.rows for celda in fila.cells]
                    elif getattr(forma, "has_text_frame", False) and forma.has_text_frame:
                        marcos = [forma.text_frame]
                    for marco in marcos:
                        if (
                            _tipo_forma(forma) == TIPO_AUTOFORMA
                            and marco.text.strip() == viejo.strip()
                            and len(nuevo) >= ETIQUETA_FACTOR_MAXIMO * len(viejo)
                            and len(nuevo) - len(viejo) > ETIQUETA_AUMENTO_MAXIMO
                        ):
                            raise EdicionRechazada(
                                f"Cambio {numero}: «{viejo}» es el texto completo de una etiqueta de la lámina {indice} "
                                f"({len(viejo)} caracteres) y «por» tiene {len(nuevo)}: no cabría y taparía lo que "
                                "tiene debajo. Esta herramienta cambia textos que ya existen; no agrega elementos "
                                "con «reemplazar». Si te pidieron agregar una tarjeta usa la operación agregar_tarjeta "
                                "({lamina, titulo, puntos}); no lo hagas reemplazando otro texto. No se cambió nada."
                            )
                        hechas, simplificado = _reemplazar_en_marco(marco, viejo, nuevo)
                        if hechas:
                            total += hechas
                            tocadas.add((lamina.slide_id, forma.shape_id))
                            if simplificado:
                                simplificados.append(indice)
            esperadas = datos.get("ocurrencias")
            if total == 0:
                raise EdicionRechazada(_no_encontrado(presentacion, numero, viejo, datos.get("lamina")))
            if esperadas is not None and int(esperadas) != total:
                raise EdicionRechazada(
                    f"Cambio {numero}: «{viejo}» aparece {total} veces y pediste {esperadas}. No se cambió nada."
                )
            detalle.append(
                {
                    "operacion": operacion,
                    "texto": viejo,
                    "por": nuevo,
                    "reemplazos": total,
                    **({"formato_simplificado_en_laminas": sorted(set(simplificados))} if simplificados else {}),
                }
            )
        elif operacion == "titulo":
            ((indice, lamina),) = _laminas_objetivo(presentacion, datos.get("lamina"))
            forma = lamina.shapes.title
            if forma is None:
                raise EdicionRechazada(f"Cambio {numero}: la lámina {indice} no tiene título editable.")
            anterior = forma.text_frame.text
            parrafo = forma.text_frame.paragraphs[0]
            if parrafo.runs:
                parrafo.runs[0].text = str(datos.get("nuevo") or "")
                for run in parrafo.runs[1:]:
                    run.text = ""
            else:
                parrafo.text = str(datos.get("nuevo") or "")
            for sobrante in forma.text_frame.paragraphs[1:]:
                for run in sobrante.runs:
                    run.text = ""
            tocadas.add((lamina.slide_id, forma.shape_id))
            detalle.append(
                {"operacion": operacion, "lamina": indice, "antes": anterior, "despues": forma.text_frame.text}
            )
        elif operacion == "agregar_tarjeta":
            detalle.append(_agregar_tarjeta(presentacion, datos, numero, tocadas))
        elif operacion == "agregar_punto":
            detalle.append(_agregar_punto(presentacion, datos, numero, tocadas))
        elif operacion == "agregar_lamina":
            detalle.append(_agregar_lamina(presentacion, datos, numero, tocadas))
        elif operacion == "notas":
            ((indice, lamina),) = _laminas_objetivo(presentacion, datos.get("lamina"))
            lamina.notes_slide.notes_text_frame.text = str(datos.get("texto") or "")
            tocadas.add((lamina.slide_id, "notas"))
            detalle.append({"operacion": operacion, "lamina": indice})
    _eliminar_laminas(presentacion, eliminadas)
    return detalle, tocadas, eliminadas


FORMAS_POR_TARJETA = 3
TOLERANCIA_FILA = 20000
TIPO_CAJA_DE_TEXTO = 17


def _con_texto(forma) -> bool:
    return bool(getattr(forma, "has_text_frame", False) and forma.has_text_frame and forma.text_frame.text.strip())


def _tarjetas(lamina, ancho_lamina: int) -> list[tuple]:
    formas = [f for f in lamina.shapes if _tipo_forma(f) in (TIPO_AUTOFORMA, TIPO_CAJA_DE_TEXTO)]
    candidatos = []
    for fondo in formas:
        if _tipo_forma(fondo) != TIPO_AUTOFORMA or _con_texto(fondo) or fondo.width < ancho_lamina // 10:
            continue
        dentro = [
            f
            for f in formas
            if f is not fondo
            and (_con_texto(f) or _tipo_forma(f) == TIPO_CAJA_DE_TEXTO)
            and fondo.left <= f.left < fondo.left + fondo.width
            and f.left + f.width <= fondo.left + fondo.width
            and fondo.top <= f.top < fondo.top + fondo.height
        ]
        etiquetas = [
            f for f in dentro if _tipo_forma(f) == TIPO_AUTOFORMA and f.left == fondo.left and f.width == fondo.width
        ]
        cuerpos = [f for f in dentro if _tipo_forma(f) == TIPO_CAJA_DE_TEXTO]
        if etiquetas and cuerpos:
            candidatos.append((fondo, etiquetas[0], cuerpos[0]))
    filas: dict[tuple, list] = {}
    for tarjeta in candidatos:
        fondo = tarjeta[0]
        clave = (round(fondo.top / TOLERANCIA_FILA), fondo.width // TOLERANCIA_FILA, fondo.height // TOLERANCIA_FILA)
        filas.setdefault(clave, []).append(tarjeta)
    mejor = max(filas.values(), key=len, default=[])
    return sorted(mejor, key=lambda t: t[0].left) if len(mejor) >= 2 else []


def _siguiente_id(lamina) -> int:
    ids = [int(e.get("id")) for e in lamina.shapes._spTree.iter(f"{NS_P}cNvPr")]  # noqa: SLF001
    return max(ids, default=1) + 1


def _poner_texto(parrafo, texto: str) -> None:
    if parrafo.runs:
        parrafo.runs[0].text = texto
        for run in parrafo.runs[1:]:
            run.text = ""
    else:
        parrafo.text = texto


def _poner_etiqueta(marco, texto: str) -> None:
    _poner_texto(marco.paragraphs[0], texto)
    for sobrante in marco.paragraphs[1:]:
        sobrante._p.getparent().remove(sobrante._p)  # noqa: SLF001


def _poner_vinetas(marco, puntos: list[str]) -> None:
    plantilla = marco.paragraphs[0]
    inicial = plantilla.text[:1]
    prefijo = ""
    if inicial in "▪•-–*":
        texto = plantilla.text
        prefijo = texto[: len(texto) - len(texto.lstrip("▪•-–* "))]
    elif not plantilla.text.strip():
        prefijo = "▪  "
    base = copy.deepcopy(plantilla._p)  # noqa: SLF001
    contenedor = plantilla._p.getparent()  # noqa: SLF001
    for parrafo in list(marco.paragraphs):
        contenedor.remove(parrafo._p)  # noqa: SLF001
    for punto in puntos:
        contenedor.append(copy.deepcopy(base))
        _poner_texto(marco.paragraphs[-1], f"{prefijo}{punto}")


def _agregar_lamina(presentacion, datos: dict, numero: int, tocadas: set) -> dict:
    titulo = str(datos.get("titulo") or "").strip()
    if not titulo:
        raise EdicionRechazada(f"Cambio {numero}: agregar_lamina necesita «titulo».")
    puntos = datos.get("puntos") or []
    if isinstance(puntos, str):
        puntos = [puntos]
    if not isinstance(puntos, list) or not all(isinstance(p, str) and p.strip() for p in puntos):
        raise EdicionRechazada(f"Cambio {numero}: «puntos» debe ser una lista de textos.")
    diseno = None
    for lamina in reversed(list(presentacion.slides)):
        if lamina.shapes.title is not None and any(p.placeholder_format.idx == 1 for p in lamina.placeholders):
            diseno = lamina.slide_layout
            break
    if diseno is None:
        for candidato in presentacion.slide_layouts:
            if candidato.placeholders and any(p.placeholder_format.idx == 1 for p in candidato.placeholders):
                diseno = candidato
                break
    if diseno is None:
        raise EdicionRechazada(
            f"Cambio {numero}: la presentación no tiene un diseño con título y cuerpo para la lámina nueva."
        )
    nueva = presentacion.slides.add_slide(diseno)
    nueva.shapes.title.text_frame.text = titulo
    cuerpo = next((p for p in nueva.placeholders if p.placeholder_format.idx == 1), None)
    if cuerpo is not None:
        marco = cuerpo.text_frame
        for indice, punto in enumerate(puntos):
            (marco.paragraphs[0] if indice == 0 else marco.add_paragraph()).text = punto.strip().lstrip("▪•-–* ")
    destino = datos.get("despues")
    lista = presentacion.slides._sldIdLst  # noqa: SLF001
    elementos = list(lista)
    if destino is not None:
        try:
            posicion = int(destino)
        except (TypeError, ValueError) as exc:
            raise EdicionRechazada(f"Cambio {numero}: «despues» debe ser un número de lámina.") from exc
        if not 0 <= posicion <= len(elementos) - 1:
            raise EdicionRechazada(f"Cambio {numero}: no existe la lámina {posicion}.")
        ultimo = elementos[-1]
        lista.remove(ultimo)
        lista.insert(posicion, ultimo)
    tocadas.add((nueva.slide_id, "lamina_nueva"))
    return {"operacion": "agregar_lamina", "lamina": len(presentacion.slides), "titulo": titulo, "puntos": len(puntos)}


def _lamina_de(presentacion, datos: dict, numero: int, operacion: str):
    if datos.get("lamina") is not None:
        ((indice, lamina),) = _laminas_objetivo(presentacion, datos.get("lamina"))
        return indice, lamina
    buscado = str(datos.get("lamina_titulo") or "").strip().lower()
    if not buscado:
        raise EdicionRechazada(f"Cambio {numero}: {operacion} necesita «lamina» (número) o «lamina_titulo».")
    coincidencias = [(i, la) for i, la in enumerate(presentacion.slides, 1) if buscado in _titulo(la).lower()]
    if len(coincidencias) != 1:
        raise EdicionRechazada(
            f"Cambio {numero}: «{buscado}» coincide con {len(coincidencias)} láminas; usa «lamina» con el número."
        )
    return coincidencias[0]


def _agregar_punto(presentacion, datos: dict, numero: int, tocadas: set) -> dict:
    texto = str(datos.get("texto") or "").strip()
    if not texto:
        raise EdicionRechazada(f"Cambio {numero}: agregar_punto necesita «texto».")
    indice, lamina = _lamina_de(presentacion, datos, numero, "agregar_punto")
    titulo = lamina.shapes.title
    candidatas = []
    for forma in _formas(lamina.shapes):
        if (titulo is not None and forma.shape_id == titulo.shape_id) or not getattr(forma, "has_text_frame", False):
            continue
        if not forma.has_text_frame:
            continue
        parrafos = [p for p in forma.text_frame.paragraphs if p.text.strip()]
        if len(parrafos) < 1:
            continue
        viñetas = sum(1 for p in parrafos if p.text.lstrip()[:1] in "▪•-–*")
        candidatas.append((viñetas, len(forma.text_frame.text), forma, parrafos))
    if not candidatas:
        raise EdicionRechazada(
            f"Cambio {numero}: la lámina {indice} no tiene una lista de texto donde agregar. No se cambió nada."
        )
    _, _, forma, parrafos = max(candidatas, key=lambda c: (c[0], c[1]))
    ultimo = parrafos[-1]
    copia = copy.deepcopy(ultimo._p)  # noqa: SLF001
    ultimo._p.addnext(copia)  # noqa: SLF001
    nuevo = next(p for p in forma.text_frame.paragraphs if p._p is copia)  # noqa: SLF001
    plano = ultimo.text
    prefijo = plano[: len(plano) - len(plano.lstrip("▪•-–* "))]
    corridas = nuevo.runs
    if len(corridas) >= 2 and corridas[0].text.strip() and corridas[0].text.strip() in "▪•-–*":
        corridas[1].text = texto
        for sobrante in corridas[2:]:
            sobrante.text = ""
    else:
        _poner_texto(nuevo, f"{prefijo}{texto}")
    tocadas.add((lamina.slide_id, forma.shape_id))
    tocadas.add((lamina.slide_id, f"par:{forma.shape_id}"))
    return {
        "operacion": "agregar_punto",
        "lamina": indice,
        "titulo_de_la_lamina": _titulo(lamina),
        "texto": texto,
        "caja": forma.name,
        "nota": f"Se agregó en la lámina {indice} «{_titulo(lamina)}». Si el usuario pidió otra lámina, el número "
        "estaba mal: usa lamina_titulo.",
    }


def _agregar_tarjeta(presentacion, datos: dict, numero: int, tocadas: set) -> dict:
    titulo = str(datos.get("titulo") or "").strip()
    puntos = [str(p).strip() for p in (datos.get("puntos") or []) if str(p).strip()]
    if not titulo or not puntos:
        raise EdicionRechazada(f"Cambio {numero}: agregar_tarjeta necesita «titulo» y «puntos» (lista no vacía).")
    if datos.get("lamina") is not None:
        ((indice, lamina),) = _laminas_objetivo(presentacion, datos.get("lamina"))
    else:
        buscado = str(datos.get("lamina_titulo") or "").strip().lower()
        if not buscado:
            raise EdicionRechazada(f"Cambio {numero}: indica «lamina» (número) o «lamina_titulo».")
        coincidencias = [(i, la) for i, la in enumerate(presentacion.slides, 1) if buscado in _titulo(la).lower()]
        if len(coincidencias) != 1:
            raise EdicionRechazada(
                f"Cambio {numero}: «{buscado}» coincide con {len(coincidencias)} láminas; usa «lamina» con el número."
            )
        indice, lamina = coincidencias[0]
    tarjetas = _tarjetas(lamina, presentacion.slide_width)
    if not tarjetas:
        raise EdicionRechazada(
            f"Cambio {numero}: la lámina {indice} «{_titulo(lamina)}» no tiene una fila de tarjetas (al menos dos del "
            "mismo tamaño con fondo, etiqueta y texto). Comprueba que sea la lámina que pidió el usuario: si era otra, "
            "usa lamina_titulo con su tema en vez del número. Si la lámina es una lista de viñetas usa agregar_punto: "
            '{"agregar_punto": {"lamina_titulo": "…", "texto": "…"}}. No se cambió nada.'
        )
    ultimo = tarjetas[-1]
    arbol = lamina.shapes._spTree  # noqa: SLF001
    identificador = _siguiente_id(lamina)
    elementos = []
    for original in ultimo:
        copia = copy.deepcopy(original._element)  # noqa: SLF001
        propiedades = next(copia.iter(f"{NS_P}cNvPr"))
        propiedades.set("id", str(identificador))
        propiedades.set("name", f"{propiedades.get('name')} (tarjeta nueva)")
        identificador += 1
        arbol.append(copia)
        elementos.append(copia)
    por_elemento = {f._element: f for f in lamina.shapes}  # noqa: SLF001
    fondo_n, etiqueta_n, cuerpo_n = (por_elemento[e] for e in elementos)
    _poner_etiqueta(etiqueta_n.text_frame, titulo)
    _poner_vinetas(cuerpo_n.text_frame, puntos)
    todas = [*tarjetas, (fondo_n, etiqueta_n, cuerpo_n)]
    inicio = tarjetas[0][0].left
    fin = ultimo[0].left + ultimo[0].width
    hueco = tarjetas[1][0].left - (tarjetas[0][0].left + tarjetas[0][0].width)
    ancho = (fin - inicio - hueco * (len(todas) - 1)) // len(todas)
    margen = ultimo[2].left - ultimo[0].left
    for k, (fondo, etiqueta, cuerpo) in enumerate(todas):
        izquierda = inicio + k * (ancho + hueco)
        if k < len(tarjetas):
            margen = cuerpo.left - fondo.left
            for forma in (fondo, etiqueta, cuerpo):
                tocadas.add((lamina.slide_id, f"mov:{forma.shape_id}"))
        for forma in (fondo, etiqueta):
            forma.left, forma.width = izquierda, ancho
        cuerpo.left, cuerpo.width = izquierda + margen, max(ancho - 2 * margen, 1)
    tocadas.add((lamina.slide_id, "tarjetas"))
    tocadas.add((lamina.slide_id, f"tarjeta:{len(tocadas)}"))
    return {
        "operacion": "agregar_tarjeta",
        "lamina": indice,
        "titulo_de_la_lamina": _titulo(lamina),
        "titulo": titulo,
        "puntos": puntos,
        "tarjetas_antes": len(tarjetas),
        "tarjetas_despues": len(todas),
    }


def _eliminar_laminas(presentacion, ids: set) -> None:
    if not ids:
        return
    lista = presentacion.slides._sldIdLst  # noqa: SLF001
    for elemento in list(lista):
        if int(elemento.get("id")) in ids:
            presentacion.part.drop_rel(elemento.rId)
            lista.remove(elemento)


def diferencias_no_pedidas(antes: list[dict], despues: list[dict], tocadas: set, eliminadas: set) -> list[str]:
    problemas = []
    nuevas_ids = {t[0] for t in tocadas if t[1] == "lamina_nueva"}
    despues = [lamina for lamina in despues if lamina["id"] not in nuevas_ids]
    esperadas = [lamina for lamina in antes if lamina["id"] not in eliminadas]
    if [lamina["id"] for lamina in esperadas] != [lamina["id"] for lamina in despues]:
        problemas.append(f"Cambió el orden o el número de láminas ({len(antes)} → {len(despues)}).")
        return problemas
    for numero, (previa, nueva) in enumerate(zip(esperadas, despues, strict=True), 1):
        ampliada = (previa["id"], "tarjetas") in tocadas
        esperado = dict(previa["conteo"])
        if ampliada:
            nuevas = sum(1 for t in tocadas if t[0] == previa["id"] and str(t[1]).startswith("tarjeta:"))
            esperado["forma"] = esperado.get("forma", 0) + FORMAS_POR_TARJETA * nuevas
        if esperado != nueva["conteo"]:
            problemas.append(f"Lámina {numero}: cambió su contenido ({esperado} → {nueva['conteo']}).")
        for forma_id, texto in previa["textos"].items():
            if (previa["id"], forma_id) in tocadas:
                continue
            if nueva["textos"].get(forma_id) != texto:
                problemas.append(f"Lámina {numero}: cambió un texto que no se pidió editar.")
        if "orden" in previa:
            orden_nuevo = nueva.get("orden") or []
            if (orden_nuevo[: len(previa["orden"])] if ampliada else orden_nuevo) != previa["orden"]:
                problemas.append(f"Lámina {numero}: cambió el orden o la cantidad de formas.")
        for forma_id, huella in (previa.get("formas") or {}).items():
            if forma_id in (nueva.get("formas") or {}) and nueva["formas"][forma_id] != huella:
                que = (
                    "el formato o la estructura de la forma editada"
                    if (previa["id"], forma_id) in tocadas
                    else ("el formato, la posición o la estructura de una forma que no se pidió editar")
                )
                problemas.append(f"Lámina {numero}: cambió {que} (forma {forma_id}).")
        if "lamina" in previa and previa["lamina"] != nueva.get("lamina"):
            problemas.append(f"Lámina {numero}: cambió el fondo, el diseño o la configuración de la lámina.")
        if "relaciones" in previa and previa["relaciones"] != nueva.get("relaciones"):
            if any("lamina:huerfana" in str(r) for r in nueva.get("relaciones") or []):
                problemas.append(
                    f"Lámina {numero}: tiene un vínculo a una lámina que se eliminaría. Quita antes ese vínculo "
                    "o no elimines esa lámina."
                )
            else:
                problemas.append(f"Lámina {numero}: cambió una imagen, un gráfico, un vínculo u otro recurso.")
        if (previa["id"], "notas") not in tocadas:
            if previa["notas"] != nueva["notas"] or previa.get("notas_huella") != nueva.get("notas_huella"):
                problemas.append(f"Lámina {numero}: cambiaron las notas sin pedirlo.")
    return problemas


def diferencias_globales(antes: dict, despues: dict) -> list[str]:
    if antes == despues:
        return []
    return ["Cambió la plantilla, un diseño, el tema o la configuración de la presentación."]


UMBRAL_SUPERPOSICION = 0.2


def _cajas_con_texto(lamina) -> dict:
    cajas = {}
    for forma in lamina.shapes:
        if not (getattr(forma, "has_text_frame", False) and forma.has_text_frame and forma.text_frame.text.strip()):
            continue
        if None in (forma.left, forma.top, forma.width, forma.height) or forma.width <= 0 or forma.height <= 0:
            continue
        cajas[forma.shape_id] = (
            forma.left,
            forma.top,
            forma.left + forma.width,
            forma.top + forma.height,
            forma.text_frame.text.strip(),
        )
    return cajas


def superposiciones(presentacion) -> set[tuple]:
    pares: set[tuple] = set()
    for numero, lamina in enumerate(presentacion.slides, start=1):
        cajas = list(_cajas_con_texto(lamina).items())
        for i in range(len(cajas)):
            for j in range(i + 1, len(cajas)):
                (ida, a), (idb, b) = cajas[i], cajas[j]
                ancho = min(a[2], b[2]) - max(a[0], b[0])
                alto = min(a[3], b[3]) - max(a[1], b[1])
                if ancho <= 0 or alto <= 0:
                    continue
                menor = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
                if menor and ancho * alto / menor > UMBRAL_SUPERPOSICION:
                    pares.add((numero, ida, idb, a[4][:40], b[4][:40]))
    return pares


def editar(origen: Path, destino_temporal: Path, cambios: list) -> dict:
    from pptx import Presentation

    comprobar_archivo(origen)
    try:
        presentacion = Presentation(str(origen))
    except Exception as exc:  # noqa: BLE001
        raise EdicionRechazada(f"No pude abrir {origen.name} como presentación: {exc}") from exc
    referencia = Presentation(str(origen))
    detalle, tocadas, eliminadas = aplicar(presentacion, cambios)
    presentacion.save(str(destino_temporal))
    guardada = Presentation(str(destino_temporal))
    antes, despues = foto(referencia, tocadas), foto(guardada, tocadas)
    problemas = diferencias_no_pedidas(antes, despues, tocadas, eliminadas)
    problemas += diferencias_globales(huella_global(referencia), huella_global(guardada))
    if problemas:
        raise EdicionRechazada("La edición cambiaría cosas no pedidas: " + " ".join(problemas))
    nuevas = sorted(
        (n, ta, tb)
        for n, ia, ib, ta, tb in superposiciones(guardada)
        if not any(x[0] == n and {x[1], x[2]} == {ia, ib} for x in superposiciones(referencia))
    )
    conteo_antes = {}
    for lamina in antes:
        for tipo, cantidad in lamina["conteo"].items():
            conteo_antes[tipo] = conteo_antes.get(tipo, 0) + cantidad
    return {
        "laminas_antes": len(antes),
        "laminas_despues": len(despues),
        "cambios": detalle,
        "conservado": {
            "titulos": [lamina["titulo"] for lamina in despues],
            "elementos_originales": conteo_antes,
        },
        **(
            {
                "advertencias": [
                    f"Lámina {n}: «{ta}» queda encima de «{tb}» (se tapan textos). Díselo al usuario; no digas que quedó perfecto."
                    for n, ta, tb in nuevas
                ]
            }
            if nuevas
            else {}
        ),
    }
