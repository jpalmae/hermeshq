import difflib
import html
import json
import re
import unicodedata
from html.parser import HTMLParser

MAX_EJEMPLOS = 12
MAX_TEXTO = 220
ENCABEZADOS = ("h1", "h2", "h3", "h4")
OMITIR = ("script", "style", "noscript", "svg", "title")
CONTAR = ("table", "svg", "img", "button", "a", "canvas", "select", "input")


def _sin_acentos(valor) -> str:
    texto = unicodedata.normalize("NFD", str(valor))
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower().strip()


def _recorte(valor, largo=MAX_TEXTO):
    texto = " ".join(str(valor).split())
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def _normal(valor):
    if valor is None or valor == "":
        return ""
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, (int, float)):
        return repr(round(float(valor), 6))
    texto = str(valor).strip()
    try:
        return repr(round(float(texto.replace(",", ".")), 6))
    except ValueError:
        return " ".join(texto.lower().split())


def _numero(valor):
    if isinstance(valor, bool) or valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None


def _columna(nombre, columnas):
    objetivo = _sin_acentos(nombre)
    for c in columnas:
        if _sin_acentos(c) == objetivo:
            return c
    for c in columnas:
        if objetivo and objetivo in _sin_acentos(c):
            return c
    return None


def comparar_tablas(a, b, clave=None, max_ejemplos=MAX_EJEMPLOS):
    cols_a, cols_b = a["columnas"], b["columnas"]
    mapa_b = {_sin_acentos(c): c for c in cols_b}
    comunes = [c for c in cols_a if _sin_acentos(c) in mapa_b]
    solo_a = [c for c in cols_a if c not in comunes]
    solo_b = [c for c in cols_b if _sin_acentos(c) not in {_sin_acentos(x) for x in comunes}]
    salida = {
        "filas_antes": len(a["registros"]),
        "filas_despues": len(b["registros"]),
        "columnas_quitadas": solo_a,
        "columnas_nuevas": solo_b,
        "columnas_comunes": len(comunes),
    }
    resumen = [f"Filas: {len(a['registros'])} → {len(b['registros'])}."]
    if solo_a:
        resumen.append("Columnas que ya no están: " + ", ".join(solo_a) + ".")
    if solo_b:
        resumen.append("Columnas nuevas: " + ", ".join(solo_b) + ".")
    if not comunes:
        salida["resumen"] = resumen + ["No hay columnas en común: no se pueden comparar las filas."]
        return salida

    def valor_b(registro, col):
        return registro.get(mapa_b[_sin_acentos(col)])

    claves = None
    if clave:
        claves = []
        for k in clave if isinstance(clave, list) else [clave]:
            real = _columna(k, comunes)
            if real is None:
                salida["error"] = (
                    f"la columna clave '{k}' no está en las dos versiones. Comunes: {', '.join(comunes[:15])}"
                )
                return salida
            claves.append(real)

    def ver(registro, cols, lado):
        return {c: _recorte(registro.get(c) if lado == "a" else valor_b(registro, c), 60) for c in cols[:6]}

    if claves:
        indice_a, indice_b, duplicadas = {}, {}, 0
        for r in a["registros"]:
            k = tuple(_normal(r.get(c)) for c in claves)
            duplicadas += k in indice_a
            indice_a.setdefault(k, r)
        for r in b["registros"]:
            k = tuple(_normal(valor_b(r, c)) for c in claves)
            duplicadas += k in indice_b
            indice_b.setdefault(k, r)
        agregadas = [k for k in indice_b if k not in indice_a]
        quitadas = [k for k in indice_a if k not in indice_b]
        modificadas, por_columna = [], {}
        for k, ra in indice_a.items():
            rb = indice_b.get(k)
            if rb is None:
                continue
            cambios = []
            for c in comunes:
                if c in claves:
                    continue
                antes, despues = ra.get(c), valor_b(rb, c)
                if _normal(antes) != _normal(despues):
                    cambios.append({"columna": c, "antes": _recorte(antes, 60), "despues": _recorte(despues, 60)})
                    por_columna[c] = por_columna.get(c, 0) + 1
            if cambios:
                modificadas.append({"clave": [_recorte(x, 40) for x in k], "cambios": cambios[:6]})
        salida.update(
            {
                "clave": claves,
                "filas_agregadas": len(agregadas),
                "filas_quitadas": len(quitadas),
                "filas_modificadas": len(modificadas),
                "claves_repetidas": duplicadas,
                "cambios_por_columna": dict(sorted(por_columna.items(), key=lambda x: -x[1])),
                "ejemplos_agregadas": [ver(indice_b[k], comunes, "b") for k in agregadas[:max_ejemplos]],
                "ejemplos_quitadas": [ver(indice_a[k], comunes, "a") for k in quitadas[:max_ejemplos]],
                "ejemplos_modificadas": modificadas[:max_ejemplos],
            }
        )
        resumen.append(
            f"Por {', '.join(claves)}: {len(agregadas)} filas agregadas, {len(quitadas)} quitadas y "
            f"{len(modificadas)} con valores cambiados."
        )
        if por_columna:
            resumen.append(
                "Columnas más modificadas: "
                + ", ".join(f"{c} ({n})" for c, n in list(salida["cambios_por_columna"].items())[:5])
                + "."
            )
        if duplicadas:
            resumen.append(f"Atención: {duplicadas} filas repiten la clave; se comparó la primera de cada una.")
    else:
        conteo_a, conteo_b, ejemplo = {}, {}, {}
        for r in a["registros"]:
            k = tuple(_normal(r.get(c)) for c in comunes)
            conteo_a[k] = conteo_a.get(k, 0) + 1
            ejemplo.setdefault(k, ver(r, comunes, "a"))
        for r in b["registros"]:
            k = tuple(_normal(valor_b(r, c)) for c in comunes)
            conteo_b[k] = conteo_b.get(k, 0) + 1
            ejemplo.setdefault(k, ver(r, comunes, "b"))
        agregadas = [(k, conteo_b[k] - conteo_a.get(k, 0)) for k in conteo_b if conteo_b[k] > conteo_a.get(k, 0)]
        quitadas = [(k, conteo_a[k] - conteo_b.get(k, 0)) for k in conteo_a if conteo_a[k] > conteo_b.get(k, 0)]
        total_agregadas, total_quitadas = sum(n for _, n in agregadas), sum(n for _, n in quitadas)
        salida.update(
            {
                "filas_agregadas": total_agregadas,
                "filas_quitadas": total_quitadas,
                "ejemplos_agregadas": [ejemplo[k] for k, _ in agregadas[:max_ejemplos]],
                "ejemplos_quitadas": [ejemplo[k] for k, _ in quitadas[:max_ejemplos]],
                "nota": "Sin 'clave' una fila editada aparece como quitada + agregada; indica 'clave' para ver qué valores cambiaron.",
            }
        )
        resumen.append(f"Filas exactas nuevas: {total_agregadas}; que ya no están: {total_quitadas}.")
    totales = []
    for c in comunes:
        va = [_numero(r.get(c)) for r in a["registros"]]
        vb = [_numero(valor_b(r, c)) for r in b["registros"]]
        va, vb = [x for x in va if x is not None], [x for x in vb if x is not None]
        if len(va) >= max(3, len(a["registros"]) // 2) and len(vb) >= 3:
            sa, sb = round(sum(va), 2), round(sum(vb), 2)
            if sa != sb:
                totales.append({"columna": c, "antes": sa, "despues": sb, "diferencia": round(sb - sa, 2)})
    if totales:
        salida["totales_numericos"] = totales[:10]
        resumen.append(
            "Totales que cambiaron: "
            + "; ".join(f"{t['columna']} {t['antes']} → {t['despues']}" for t in totales[:5])
            + "."
        )
    salida["resumen"] = resumen
    return salida


class _Lector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.secciones = [["(inicio)", []]]
        self.pila = []
        self.omitir = 0
        self.en_encabezado = None
        self.conteo = {t: 0 for t in CONTAR}
        self.estilos = []
        self.spec = None
        self._script_spec = False
        self._en_style = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in self.conteo:
            self.conteo[tag] += 1
        if tag == "script":
            self._script_spec = d.get("id") == "hq-spec"
        if tag == "style":
            self._en_style = True
        if tag in OMITIR:
            self.omitir += 1
            return
        if tag in ENCABEZADOS:
            self.en_encabezado = []
        elif tag in ("p", "li", "tr", "div", "br", "td", "th"):
            self.secciones[-1][1].append(" ")

    def handle_endtag(self, tag):
        if tag in OMITIR:
            self.omitir = max(0, self.omitir - 1)
        if tag == "style":
            self._en_style = False
        if tag == "script":
            self._script_spec = False
        if tag in ENCABEZADOS and self.en_encabezado is not None:
            titulo = " ".join("".join(self.en_encabezado).split())
            if titulo:
                self.secciones.append([titulo, []])
            self.en_encabezado = None

    def handle_data(self, data):
        if self._script_spec:
            try:
                self.spec = json.loads(data)
            except ValueError:
                self.spec = None
            return
        if self._en_style:
            self.estilos.append(data)
            return
        if self.omitir:
            return
        if self.en_encabezado is not None:
            self.en_encabezado.append(data)
        else:
            self.secciones[-1][1].append(data)


def leer_html(texto):
    lector = _Lector()
    lector.feed(texto)
    lector.close()
    secciones = []
    for titulo, partes in lector.secciones:
        cuerpo = " ".join("".join(partes).split())
        if titulo != "(inicio)" or cuerpo:
            secciones.append((titulo, cuerpo))
    variables = {}
    for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;}{]+)", "\n".join(lector.estilos), re.I):
        variables.setdefault(m.group(1), m.group(2).strip())
    return {"secciones": secciones, "conteo": lector.conteo, "variables": variables, "spec": lector.spec}


def _frases(texto):
    partes = re.split(r"(?<=[.!?:;])\s+|\s{2,}", texto)
    return [p.strip() for p in partes if len(p.strip()) >= 3]


def _emparejar(secc_a, secc_b):
    usadas, pares = set(), []
    por_titulo = {}
    for i, (t, _) in enumerate(secc_b):
        por_titulo.setdefault(_sin_acentos(t), []).append(i)
    pendientes_a = []
    for ia, (ta, _) in enumerate(secc_a):
        candidatos = [j for j in por_titulo.get(_sin_acentos(ta), []) if j not in usadas]
        if candidatos:
            usadas.add(candidatos[0])
            pares.append((ia, candidatos[0]))
        else:
            pendientes_a.append(ia)
    libres_b = [j for j in range(len(secc_b)) if j not in usadas]
    quitadas = []
    for ia in pendientes_a:
        mejor, puntaje = None, 0.0
        ta, ca = _sin_acentos(secc_a[ia][0]), _sin_acentos(secc_a[ia][1])[:600]
        for j in libres_b:
            tb, cb = _sin_acentos(secc_b[j][0]), _sin_acentos(secc_b[j][1])[:600]
            titulo = difflib.SequenceMatcher(None, ta, tb).ratio()
            if ta and tb and (ta in tb or tb in ta):
                titulo = max(titulo, 0.85)
            cuerpo = difflib.SequenceMatcher(None, ca, cb).ratio() if len(ca) > 40 and len(cb) > 40 else 0.0
            p = max(titulo if titulo >= 0.75 else 0.0, cuerpo if cuerpo >= 0.6 else 0.0)
            if p > puntaje:
                mejor, puntaje = j, p
        if mejor is not None:
            libres_b.remove(mejor)
            pares.append((ia, mejor))
        else:
            quitadas.append(ia)
    return pares, quitadas, libres_b


def comparar_html(texto_a, texto_b, max_ejemplos=MAX_EJEMPLOS):
    a, b = leer_html(texto_a), leer_html(texto_b)
    pares, quitadas, nuevas = _emparejar(a["secciones"], b["secciones"])
    modificadas, iguales = [], 0
    for ia, ib in sorted(pares):
        (ta, ca), (tb, cb) = a["secciones"][ia], b["secciones"][ib]
        fa, fb = _frases(ca), _frases(cb)
        sa, sb = set(fa), set(fb)
        agregadas, retiradas = [f for f in fb if f not in sa], [f for f in fa if f not in sb]
        renombrada = _sin_acentos(ta) != _sin_acentos(tb)
        if not agregadas and not retiradas and not renombrada:
            iguales += 1
            continue
        item = {"seccion": tb, "frases_nuevas": len(agregadas), "frases_quitadas": len(retiradas)}
        if renombrada:
            item["antes_se_llamaba"] = ta
        item["nuevas"] = [_recorte(f) for f in agregadas[:4]]
        item["quitadas"] = [_recorte(f) for f in retiradas[:4]]
        modificadas.append(item)
    estilo = {
        k: {"antes": a["variables"].get(k), "despues": b["variables"].get(k)}
        for k in sorted(set(a["variables"]) | set(b["variables"]))
        if a["variables"].get(k) != b["variables"].get(k)
    }
    estructura = {
        k: {"antes": a["conteo"][k], "despues": b["conteo"][k]} for k in CONTAR if a["conteo"][k] != b["conteo"][k]
    }
    resumen = [
        f"Secciones: {len(a['secciones'])} → {len(b['secciones'])}; sin cambios: {iguales}; modificadas: {len(modificadas)}; "
        f"nuevas: {len(nuevas)}; quitadas: {len(quitadas)}."
    ]
    if nuevas:
        resumen.append("Secciones nuevas: " + "; ".join(_recorte(b["secciones"][j][0], 70) for j in nuevas[:6]) + ".")
    if quitadas:
        resumen.append(
            "Secciones que ya no están: " + "; ".join(_recorte(a["secciones"][i][0], 70) for i in quitadas[:6]) + "."
        )
    for m in modificadas[:6]:
        resumen.append(
            f"«{_recorte(m['seccion'], 60)}»: {m['frases_nuevas']} frases nuevas y {m['frases_quitadas']} quitadas"
            + (f" (antes «{_recorte(m['antes_se_llamaba'], 50)}»)" if m.get("antes_se_llamaba") else "")
            + "."
        )
    if estilo:
        resumen.append(f"Estilo: cambiaron {len(estilo)} variables de diseño (colores, tamaños).")
    if estructura:
        resumen.append(
            "Estructura: "
            + ", ".join(f"{k} {v['antes']} → {v['despues']}" for k, v in list(estructura.items())[:6])
            + "."
        )
    return {
        "secciones_antes": len(a["secciones"]),
        "secciones_despues": len(b["secciones"]),
        "secciones_sin_cambios": iguales,
        "secciones_nuevas": [b["secciones"][j][0] for j in nuevas[:max_ejemplos]],
        "secciones_quitadas": [a["secciones"][i][0] for i in quitadas[:max_ejemplos]],
        "secciones_modificadas": modificadas[:max_ejemplos],
        "variables_de_estilo_cambiadas": dict(list(estilo.items())[:12]),
        "estructura": estructura,
        "spec_embebido": {"antes": a["spec"] is not None, "despues": b["spec"] is not None},
        "resumen": resumen,
    }


def titulo_html(texto):
    m = re.search(r"<title>(.*?)</title>", texto, re.S | re.I)
    return html.unescape(m.group(1)).strip() if m else ""
