from __future__ import annotations

import json
import re
import unicodedata
from datetime import timedelta
from pathlib import Path

VERSION = 1
VENTANA_IMPLICITA = timedelta(hours=12)
MAX_RESUMENES_CANDIDATOS = 20
MAX_ELEMENTOS = 60
HERRAMIENTA = "ms365_correo_sync"
ARCHIVO_ESTADO = Path("work") / "correo_unificado_estado.json"
RESPALDOS_ESTADO = 3
CATEGORIAS = ("INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO", "FRÍO")

_FILA_CON_ID = re.compile(r"^\s*\|\s*(#\d+)\s*\|")
_HORA = re.compile(r"\b([01]?\d|2[0-3])[:.h]([0-5]\d)\b")
_ID_VISIBLE = re.compile(r"#\s*(\d{1,7})\b")
_ORDINAL_NUMERICO = re.compile(r"\b(\d{1,3})(?:º|°|ª|o\b|a\b|er|ro|ra|do|da|to|ta|vo|va|no|na|mo|ma)\b")
_POSICION_EXPLICITA = re.compile(r"\b(?:posici[oó]n|lugar|fila)\s*(?:n[uú]mero\s*|n[º°o.]\s*)?(\d{1,3})\b")
_NUMERO_SUELTO = re.compile(r"(?<![#\d])\b(\d{1,7})\b")
_FRASE_REMITENTE = (
    re.compile(
        r"\b(?:el|la|lo|los|las|correo|correos|mail|mails|mensaje|mensajes|email|emails|primero|primera|segundo|"
        r"segunda|tercero|tercera|cuarto|cuarta|quinto|quinta|ultimo|ultima|penultimo|penultima)\s+del?\s+(.+)$"
    ),
    re.compile(r"\bque\s+(?:me\s+|nos\s+)?(?:mand[oó]|envi[oó]|escribi[oó]|lleg[oó] de)\s+(.+)$"),
)
_ORDINALES = {
    "primer": 1,
    "primero": 1,
    "primera": 1,
    "segundo": 2,
    "segunda": 2,
    "tercer": 3,
    "tercero": 3,
    "tercera": 3,
    "cuarto": 4,
    "cuarta": 4,
    "quinto": 5,
    "quinta": 5,
    "sexto": 6,
    "sexta": 6,
    "septimo": 7,
    "septima": 7,
    "setimo": 7,
    "setima": 7,
    "octavo": 8,
    "octava": 8,
    "noveno": 9,
    "novena": 9,
    "decimo": 10,
    "decima": 10,
    "ultimo": -1,
    "ultima": -1,
    "penultimo": -2,
    "penultima": -2,
}
_CORTE_NOMBRE = {
    "que",
    "sobre",
    "con",
    "y",
    "para",
    "en",
    "por",
    "del",
    "de",
    "resumen",
    "informe",
    "lista",
    "tabla",
    "arriba",
    "abajo",
    "antes",
    "hoy",
    "ayer",
    "esta",
    "este",
    "manana",
    "tarde",
    "noche",
    "las",
    "los",
    "a",
    "al",
    "o",
    "u",
    "pero",
    "porfa",
    "favor",
    "correo",
    "mail",
}
_ARTICULOS = {"el", "la", "lo", "los", "las", "don", "dona", "sr", "sra", "senor", "senora"}


def plano(texto) -> str:
    sin_tildes = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", sin_tildes).strip().lower()


def palabras(texto) -> list[str]:
    return re.findall(r"[a-z0-9]+", plano(texto))


def numero_visible(display_id) -> int | None:
    coincide = re.search(r"\d+", str(display_id or ""))
    return int(coincide.group()) if coincide else None


def _digest_del_commit(messages: list | None) -> dict | None:
    digest = None
    for mensaje in messages or []:
        if not isinstance(mensaje, dict) or mensaje.get("role") != "tool" or mensaje.get("name") != HERRAMIENTA:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(resultado, dict) and resultado.get("success") and isinstance(resultado.get("tablas_md"), dict):
            digest = resultado
    return digest


def _ids_en_tablas(texto: str | None) -> list[str]:
    vistos: list[str] = []
    for linea in str(texto or "").splitlines():
        coincide = _FILA_CON_ID.match(linea)
        if coincide and coincide.group(1) not in vistos:
            vistos.append(coincide.group(1))
    return vistos


def _orden_del_digest(digest: dict) -> list[str]:
    orden = [str(x) for x in digest.get("orden_visible") or [] if x]
    if orden:
        return orden
    filas = [f for f in digest.get("filas_nuevas") or [] if isinstance(f, dict)]
    salida: list[str] = []
    for categoria in CATEGORIAS:
        for fila in filas:
            if plano(fila.get("categoria")) == plano(categoria) and fila.get("id") and fila["id"] not in salida:
                salida.append(str(fila["id"]))
    return salida


def _leer_estado(workspace: Path | None) -> dict:
    if workspace is None:
        return {}
    principal = Path(workspace) / ARCHIVO_ESTADO
    candidatos = [principal] + [principal.parent / f"{principal.name}.bak-{n}" for n in range(1, RESPALDOS_ESTADO + 1)]
    for ruta in candidatos:
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(datos, dict):
            return datos
    return {}


def _indice_del_estado(estado: dict) -> dict[str, dict]:
    indice: dict[str, dict] = {}
    for seccion in ("resolved", "discarded", "pending"):
        for item in estado.get(seccion) or []:
            if isinstance(item, dict) and item.get("display_id"):
                indice[str(item["display_id"])] = item
    return indice


def extraer_resumen(respuesta: str | None, messages: list | None, workspace: Path | None) -> dict | None:
    digest = _digest_del_commit(messages)
    if digest is None:
        return None
    filas: dict[str, dict] = {}
    for clave in ("filas_nuevas", "otros_relevantes", "filas"):
        for fila in digest.get(clave) or []:
            if isinstance(fila, dict) and fila.get("id") and str(fila["id"]) not in filas:
                filas[str(fila["id"])] = fila
    orden = [i for i in _ids_en_tablas(respuesta) if i in filas] or [i for i in _orden_del_digest(digest) if i in filas]
    indice = _indice_del_estado(_leer_estado(workspace))
    elementos = []
    for posicion, display_id in enumerate(orden[:MAX_ELEMENTOS], start=1):
        fila = filas[display_id]
        guardado = indice.get(display_id) or {}
        coincide = bool(guardado) and plano(guardado.get("subject"))[:200] == plano(fila.get("asunto"))[:200]
        elementos.append(
            {
                "posicion": posicion,
                "display_id": display_id,
                "message_id": str(guardado.get("message_id") or "") if coincide else "",
                "internet_message_id": str(guardado.get("internet_message_id") or "") if coincide else "",
                "asunto": str(fila.get("asunto") or "")[:200],
                "remitente": str(fila.get("remitente") or "")[:120],
                "remitente_correo": str(guardado.get("sender_address") or "")[:160] if coincide else "",
                "categoria": str(fila.get("categoria") or ""),
                "verificado": coincide and bool(guardado.get("message_id")),
            }
        )
    if not elementos:
        return None
    return {"version": VERSION, "elementos": elementos, "total": len(orden)}


def visibles_en(elementos: list[dict], cuerpo_entregado: str | None) -> list[dict]:
    if not cuerpo_entregado:
        return list(elementos)
    vistos = set(_ids_en_tablas(cuerpo_entregado)) or {
        e["display_id"] for e in elementos if re.search(re.escape(e["display_id"]) + r"\b", cuerpo_entregado)
    }
    if not vistos:
        return list(elementos)
    return [e for e in elementos if e["display_id"] in vistos]


def _nombre_tras(frase: str) -> list[str]:
    lista = palabras(frase)
    while lista and lista[0] in _ARTICULOS:
        lista = lista[1:]
    nombre: list[str] = []
    for palabra in lista:
        if palabra in _CORTE_NOMBRE or palabra in _ORDINALES or palabra.isdigit():
            break
        nombre.append(palabra)
        if len(nombre) == 3:
            break
    return nombre


def interpretar(texto: str | None) -> dict:
    crudo = str(texto or "")
    criterios: dict = {}
    sin_horas = _HORA.sub(" ", crudo)
    ids = [int(m) for m in _ID_VISIBLE.findall(sin_horas)]
    if ids:
        criterios["display_ids"] = ids
    base = plano(sin_horas)
    posiciones = [int(m) for m in _POSICION_EXPLICITA.findall(base)]
    posiciones += [int(m) for m in _ORDINAL_NUMERICO.findall(base)]
    posiciones += [_ORDINALES[p] for p in re.findall(r"[a-z]+", base) if p in _ORDINALES]
    if posiciones:
        criterios["posiciones"] = list(dict.fromkeys(posiciones))
    resto = _ID_VISIBLE.sub(" ", sin_horas)
    resto = _POSICION_EXPLICITA.sub(" ", plano(resto))
    resto = _ORDINAL_NUMERICO.sub(" ", resto)
    sueltos = [int(m) for m in _NUMERO_SUELTO.findall(resto)]
    if sueltos:
        criterios["numeros"] = list(dict.fromkeys(sueltos))
    for patron in _FRASE_REMITENTE:
        coincide = patron.search(base)
        if coincide:
            nombre = _nombre_tras(coincide.group(1))
            if nombre:
                criterios["nombre"] = nombre
                break
    return criterios


def _de_quien(elemento: dict, nombre: list[str]) -> bool:
    remitente = set(palabras(elemento.get("remitente"))) | set(
        palabras(str(elemento.get("remitente_correo") or "").split("@")[0].replace(".", " "))
    )
    return all(parte in remitente for parte in nombre)


def _sobre(elemento: dict, nombre: list[str]) -> bool:
    asunto = set(palabras(elemento.get("asunto")))
    return all(parte in asunto for parte in nombre)


def _por_posicion(elementos: list[dict], posicion: int) -> dict | None:
    if posicion < 0:
        return elementos[posicion] if len(elementos) >= -posicion else None
    return elementos[posicion - 1] if 1 <= posicion <= len(elementos) else None


def resolver(elementos: list[dict], criterios: dict) -> dict:
    if not elementos:
        return {"estado": "sin_coincidencia", "motivo": "ese resumen no tiene correos identificables"}
    nombre = criterios.get("nombre") or []
    universo = list(elementos)
    por_nombre = False
    if nombre:
        del_remitente = [e for e in elementos if _de_quien(e, nombre)]
        del_asunto = [e for e in elementos if _sobre(e, nombre)] if not del_remitente else []
        filtrados = del_remitente or del_asunto
        if filtrados:
            universo, por_nombre = filtrados, True
        else:
            return {
                "estado": "sin_coincidencia",
                "motivo": f"no hay ningún correo de «{' '.join(nombre)}» en ese resumen",
            }
    candidatos: list[dict] = []
    for numero in criterios.get("display_ids") or []:
        candidatos += [e for e in universo if numero_visible(e["display_id"]) == numero]
    if criterios.get("display_ids") and not candidatos:
        visibles = ", ".join(e["display_id"] for e in elementos[:12])
        return {
            "estado": "sin_coincidencia",
            "motivo": f"el número {', '.join('#' + str(n) for n in criterios['display_ids'])} no está en ese "
            f"resumen (tiene {visibles})",
        }
    if not criterios.get("display_ids"):
        for posicion in criterios.get("posiciones") or []:
            elegido = _por_posicion(universo, posicion)
            if elegido is None:
                return {
                    "estado": "sin_coincidencia",
                    "motivo": f"no existe la posición {posicion} (hay {len(universo)} correo(s)"
                    + (f" de «{' '.join(nombre)}»" if por_nombre else "")
                    + " en ese resumen)",
                }
            candidatos.append(elegido)
        if not criterios.get("posiciones"):
            for numero in criterios.get("numeros") or []:
                por_id = [e for e in universo if numero_visible(e["display_id"]) == numero]
                por_lugar = _por_posicion(universo, numero) if numero > 0 else None
                candidatos += por_id + ([por_lugar] if por_lugar is not None else [])
            if criterios.get("numeros") and not candidatos:
                return {
                    "estado": "sin_coincidencia",
                    "motivo": f"ningún correo de ese resumen tiene el número ni la posición {criterios['numeros'][0]}",
                }
    if not candidatos and por_nombre:
        candidatos = universo
    unicos = list({e["display_id"]: e for e in candidatos}.values())
    if not unicos:
        return {"estado": "sin_referencia"}
    if len(unicos) > 1:
        motivo = (
            f"hay {len(unicos)} correos de «{' '.join(nombre)}»"
            if nombre and not any(criterios.get(k) for k in ("display_ids", "posiciones", "numeros"))
            else "la referencia calza con más de un correo"
        )
        return {"estado": "ambiguo", "motivo": motivo, "opciones": unicos[:10]}
    return {"estado": "resuelto", "correo": unicos[0]}


def vista_corta(elemento: dict) -> dict:
    return {
        k: elemento.get(k) for k in ("posicion", "display_id", "asunto", "remitente", "remitente_correo", "categoria")
    }


def hora_indicada(texto: str | None) -> str | None:
    coincide = _HORA.search(str(texto or ""))
    return f"{int(coincide.group(1)):02d}:{coincide.group(2)}" if coincide else None


ORIGEN_CHAT = "chat"
CLAVE_LISTADO_CHAT = "listado_correo_chat"
CLAVE_REFERENCIA_CHAT = "referencia_correo_chat"
HERRAMIENTAS_LISTADO_CHAT = ("ms365_mail_list", "ms365_mail_search")
UMBRAL_ALINEACION = 0.6
MAX_TEXTO_REFERENCIA = 160
_PREFIJOS_ASUNTO = {"re", "rv", "fw", "fwd", "tr", "res", "aw", "wg"}
_ITEM_NUMERADO = re.compile(
    r"^\s*(?:[-*•]\s+)?[*_]{0,2}(?:#(\d{1,3})[*_]{0,2}[.):\-–—]?|(\d{1,3})[*_]{0,2}[.):\-–—])[*_]{0,2}\s+(.+)$"
)
_CELDA_NUMERO = re.compile(r"^[*_]{0,2}#?(\d{1,3})[.)]?[*_]{0,2}$")
_CELDA_SEPARADOR = re.compile(r"^:?-{2,}:?$")
_SOLO_NUMERO = re.compile(r"^(?:[a-z]+\s+){0,3}(?:el|la|n[uú]mero|numero|nro\.?|n[º°o.])?\s*\d{1,3}\s*[?.!]*$")
_PLURAL_O_BUSQUEDA = re.compile(
    r"\b(correos|mails|mensajes|emails|los|las|todos|todas|busca|buscar|buscame|trae|traeme|lista|listar|listame)\b"
)


def _nombres_de_llamadas(messages: list) -> dict[str, str]:
    nombres: dict[str, str] = {}
    for mensaje in messages:
        if isinstance(mensaje, dict):
            for llamada in mensaje.get("tool_calls") or []:
                if isinstance(llamada, dict) and llamada.get("id"):
                    nombres[str(llamada["id"])] = str((llamada.get("function") or {}).get("name") or "")
    return nombres


def _correo_de_graph(crudo: dict) -> dict:
    origen = crudo.get("from") or crudo.get("sender") or {}
    direccion = origen.get("emailAddress") if isinstance(origen, dict) else {}
    direccion = direccion if isinstance(direccion, dict) else {}
    return {
        "message_id": str(crudo.get("id") or ""),
        "internet_message_id": str(crudo.get("internetMessageId") or ""),
        "asunto": str(crudo.get("subject") or "")[:200],
        "remitente": str(direccion.get("name") or direccion.get("address") or "")[:120],
        "remitente_correo": str(direccion.get("address") or "")[:160],
        "fecha": str(crudo.get("receivedDateTime") or ""),
    }


def correos_de_herramientas(messages: list | None) -> list[dict]:
    lista = [m for m in messages or [] if isinstance(m, dict)]
    nombres = _nombres_de_llamadas(lista)
    correos: dict[str, dict] = {}
    for mensaje in lista:
        if mensaje.get("role") != "tool":
            continue
        nombre = str(mensaje.get("name") or nombres.get(str(mensaje.get("tool_call_id") or ""), ""))
        if nombre not in HERRAMIENTAS_LISTADO_CHAT:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(resultado, dict) or not resultado.get("success"):
            continue
        for crudo in resultado.get("messages") or []:
            if isinstance(crudo, dict) and crudo.get("id") and str(crudo["id"]) not in correos:
                correos[str(crudo["id"])] = _correo_de_graph(crudo)
    return list(correos.values())


def _celdas(linea: str) -> list[str]:
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def _es_separador(fila: list[str]) -> bool:
    return all(_CELDA_SEPARADOR.match(c.replace(" ", "")) for c in fila if c)


def _filas_de_tabla(bloque: list[list[str]]) -> tuple[list[tuple[int, str]], list[str]]:
    if len(bloque) >= 2 and _es_separador(bloque[1]):
        bloque = bloque[2:]
    numerados: list[tuple[int, str]] = []
    sin_numero: list[str] = []
    for fila in bloque:
        if _es_separador(fila):
            continue
        numero = _CELDA_NUMERO.match(fila[0]) if fila else None
        if numero and len(fila) > 1:
            numerados.append((int(numero.group(1)), " ".join(fila[1:])))
        else:
            sin_numero.append(" ".join(fila))
    return numerados, sin_numero


def items_del_listado(respuesta: str | None) -> list[tuple[int, str]]:
    numerados: list[tuple[int, str]] = []
    tablas_sin_numero: list[list[str]] = []
    bloque: list[list[str]] = []
    for linea in str(respuesta or "").splitlines() + [""]:
        if linea.strip().startswith("|"):
            bloque.append(_celdas(linea))
            continue
        if bloque:
            filas, sin_numero = _filas_de_tabla(bloque)
            numerados += filas
            if sin_numero:
                tablas_sin_numero.append(sin_numero)
            bloque = []
        coincide = _ITEM_NUMERADO.match(linea)
        if coincide:
            numerados.append((int(coincide.group(1) or coincide.group(2)), coincide.group(3)))
    if numerados:
        secuencia: list[tuple[int, str]] = []
        for numero, texto in numerados:
            if numero == len(secuencia) + 1:
                secuencia.append((numero, texto))
        return secuencia
    if len(tablas_sin_numero) == 1:
        return list(enumerate(tablas_sin_numero[0], start=1))
    return []


def _significativas(texto) -> list[str]:
    todas = [p for p in palabras(texto) if p not in _PREFIJOS_ASUNTO]
    largas = [p for p in todas if len(p) > 2]
    return (largas or todas)[:8]


def _presente(palabra: str, linea: set[str]) -> bool:
    return palabra in linea or (len(palabra) > 3 and any(len(w) >= 4 and palabra.startswith(w) for w in linea))


def _puntaje(correo: dict, texto: str) -> tuple[float, bool]:
    linea = set(palabras(texto))
    remitente = _significativas(correo.get("remitente"))
    del_remitente = bool(remitente) and all(_presente(p, linea) for p in remitente)
    asunto = _significativas(correo.get("asunto"))
    if not asunto:
        return (1.0 if "sin asunto" in plano(texto) and del_remitente else 0.0), del_remitente
    aciertos = [_presente(p, linea) for p in asunto]
    prefijo = next((i for i, ok in enumerate(aciertos) if not ok), len(aciertos))
    if prefijo >= min(3, len(asunto)):
        return 1.0, del_remitente
    return sum(aciertos) / len(asunto), del_remitente


def _alinear(texto: str, correos: list[dict], usados: set[str]) -> dict | None:
    mejor: tuple | None = None
    elegido = None
    for correo in correos:
        if correo["message_id"] in usados:
            continue
        puntaje, del_remitente = _puntaje(correo, texto)
        if puntaje < UMBRAL_ALINEACION:
            continue
        clave = (puntaje, del_remitente)
        if mejor is None or clave > mejor:
            mejor, elegido = clave, correo
    return elegido


def extraer_listado_chat(respuesta: str | None, messages: list | None) -> dict | None:
    correos = correos_de_herramientas(messages)
    if not correos:
        return None
    items = items_del_listado(respuesta)
    if not items:
        return None
    usados: set[str] = set()
    elementos = []
    for posicion, texto in items[:MAX_ELEMENTOS]:
        correo = _alinear(texto, correos, usados)
        if correo is None:
            return None
        usados.add(correo["message_id"])
        elementos.append(
            {
                "posicion": posicion,
                "display_id": f"#{posicion}",
                "message_id": correo["message_id"],
                "internet_message_id": correo["internet_message_id"],
                "asunto": correo["asunto"],
                "remitente": correo["remitente"],
                "remitente_correo": correo["remitente_correo"],
                "fecha": correo["fecha"],
                "categoria": "",
                "verificado": True,
            }
        )
    return {"version": VERSION, "origen": ORIGEN_CHAT, "elementos": elementos, "total": len(items)}


def _limpio(texto) -> str:
    return " ".join(str(texto or "").split()).replace("[", "(").replace("]", ")")


def criterios_de_referencia(texto: str) -> dict | None:
    base = plano(texto)
    if not base or base.startswith("/") or len(base) > MAX_TEXTO_REFERENCIA:
        return None
    criterios = interpretar(texto)
    if criterios.get("display_ids") or criterios.get("posiciones"):
        return criterios
    if criterios.get("nombre"):
        return None if _PLURAL_O_BUSQUEDA.search(base) else criterios
    if criterios.get("numeros") and _SOLO_NUMERO.match(base):
        return criterios
    return None


def _por_separado(criterios: dict) -> list[dict]:
    nombre = {"nombre": criterios["nombre"]} if criterios.get("nombre") else {}
    if len(criterios.get("display_ids") or []) > 1:
        return [{**nombre, "display_ids": [n]} for n in criterios["display_ids"]]
    if not criterios.get("display_ids") and len(criterios.get("posiciones") or []) > 1:
        return [{**nombre, "posiciones": [p]} for p in criterios["posiciones"]]
    return [criterios]


def _descripcion(elemento: dict, con_id: bool) -> str:
    texto = f"#{elemento['posicion']} «{_limpio(elemento.get('asunto')) or '(sin asunto)'}»"
    if elemento.get("remitente"):
        texto += f" de {_limpio(elemento['remitente'])}"
    if con_id:
        texto += f' (usa message_id="#{elemento["posicion"]}")'
    return texto


def referencia_en_listado(elementos: list[dict], texto: str | None) -> dict | None:
    criterios = criterios_de_referencia(str(texto or ""))
    if not criterios or not elementos:
        return None
    resueltos = []
    for parte in _por_separado(criterios):
        resultado = resolver(elementos, parte)
        estado = resultado["estado"]
        if estado == "resuelto":
            resueltos.append(resultado["correo"])
            continue
        if estado == "ambiguo":
            opciones = "; ".join(_descripcion(e, False) for e in resultado["opciones"])
            return {
                "linea": f"[Referencia: «{_limpio(texto)}» puede ser más de un correo de la lista que le mostraste: "
                f"{opciones}. Pregúntale cuál antes de abrir; no elijas tú.]",
                "correos": [],
            }
        if estado == "sin_coincidencia" and (parte.get("display_ids") or parte.get("posiciones")):
            return {
                "linea": f"[Referencia: la lista de correos que le mostraste tiene {len(elementos)} correo(s) "
                f"(#1 a #{len(elementos)}); «{_limpio(texto)}» no corresponde a ninguno. No abras un correo a "
                "ciegas: díselo y ofrécele volver a listar.]",
                "correos": [],
            }
        return None
    if not resueltos:
        return None
    vistos = list({e["message_id"]: e for e in resueltos}.values())
    if len(vistos) == 1:
        e = vistos[0]
        linea = (
            f"[Referencia: el #{e['posicion']} de la lista de correos que le mostraste es "
            f"«{_limpio(e.get('asunto')) or '(sin asunto)'}»"
            + (f" de {_limpio(e['remitente'])}" if e.get("remitente") else "")
            + f'. Ábrelo directamente con ms365_mail_get usando message_id="#{e["posicion"]}" (la plataforma lo '
            "traduce al id exacto; no copies ids largos); no preguntes si debes abrirlo.]"
        )
    else:
        detalle = "; ".join(_descripcion(e, True) for e in vistos)
        linea = (
            f"[Referencia: de la lista de correos que le mostraste: {detalle}. Ábrelos directamente con "
            "ms365_mail_get usando los «#N» como message_id (la plataforma los traduce al id exacto; no copies ids "
            "largos); no preguntes si debes abrirlos.]"
        )
    return {
        "linea": linea,
        "correos": [
            {"posicion": e["posicion"], "message_id": e["message_id"], "asunto": e.get("asunto") or ""} for e in vistos
        ],
    }
