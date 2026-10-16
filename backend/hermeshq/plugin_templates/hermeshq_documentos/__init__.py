from __future__ import annotations

import csv
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path


def _skills_root() -> Path | None:
    candidatos = [os.environ.get("HERMESHQ_SKILL_TEMPLATES", ""), "/app/hermeshq/skill_templates"]
    for candidato in candidatos:
        if candidato and Path(candidato).is_dir():
            return Path(candidato)
    return None


def _workspace() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD", os.getcwd()))


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(texto).lower()).strip("-")[:40] or "documento"


EXTENSIONES_DATOS = (".xlsx", ".xlsm", ".csv", ".xls", ".xlsb", ".ods")


FORMATOS_ANTIGUOS = (".xls", ".xlsb", ".ods")


MAX_RESUMEN = 7000


MAX_FILAS_VER = 60


def _valor(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _numero_en_texto(v):
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v):
        numero = float(v)
        return int(numero) if numero.is_integer() and "." not in v else numero
    return v


def _vacio(v):
    return v is None or v == ""


def _hash_archivo(ruta: Path) -> str:
    return hashlib.sha1(ruta.read_bytes()).hexdigest()[:12]


def _filas_hoja(hoja):
    filas = []
    for fila in hoja.iter_rows(values_only=True):
        valores = [_valor(c) for c in fila]
        while valores and _vacio(valores[-1]):
            valores.pop()
        filas.append(valores)
    while filas and not filas[-1]:
        filas.pop()
    return filas


def _fila_encabezado(filas):
    mejor, puntaje = 0, -1
    for indice, fila in enumerate(filas[:15]):
        textos = [c for c in fila if isinstance(c, str) and c]
        if len(textos) < 2:
            continue
        siguiente = filas[indice + 1] if indice + 1 < len(filas) else []
        if len([c for c in siguiente if not _vacio(c)]) == 0:
            continue
        if len(textos) > puntaje:
            mejor, puntaje = indice, len(textos)
    return mejor


def _nombres_columnas(fila, ancho):
    nombres, usados = [], set()
    for i in range(ancho):
        base = fila[i] if i < len(fila) and not _vacio(fila[i]) else f"col{i + 1}"
        nombre = str(base)
        while nombre in usados:
            nombre += "_"
        usados.add(nombre)
        nombres.append(nombre)
    return nombres


def _tabla_desde_filas(nombre, filas):
    if not filas:
        return {"nombre": nombre, "filas": 0, "columnas": [], "registros": [], "notas": []}
    idx = _fila_encabezado(filas)
    notas = [" | ".join(str(c) for c in f if not _vacio(c)) for f in filas[:idx] if any(not _vacio(c) for c in f)]
    cuerpo = [f for f in filas[idx + 1 :] if any(not _vacio(c) for c in f)]
    ancho = max([len(filas[idx])] + [len(f) for f in cuerpo[:200]] or [0])
    columnas = _nombres_columnas(filas[idx], ancho)
    utiles = [
        i
        for i, nombre_col in enumerate(columnas)
        if not nombre_col.startswith("col") or any(i < len(f) and not _vacio(f[i]) for f in cuerpo)
    ]
    registros = []
    for fila in cuerpo:
        registro = {columnas[i]: (fila[i] if i < len(fila) and not _vacio(fila[i]) else None) for i in utiles}
        registros.append(registro)
    return {
        "nombre": nombre,
        "fila_encabezado": idx + 1,
        "filas": len(registros),
        "columnas": [columnas[i] for i in utiles],
        "notas": notas[:6],
        "registros": registros,
    }


def _convertir_a_xlsx(ruta: Path, carpeta: Path) -> Path:
    oficina = shutil.which("soffice") or shutil.which("libreoffice")
    if not oficina:
        raise ValueError(f"no hay conversor instalado para leer archivos {ruta.suffix}")
    origen = carpeta / ("origen" + ruta.suffix.lower())
    shutil.copy2(ruta, origen)
    subprocess.run(
        [oficina, "--headless", "--convert-to", "xlsx", "--outdir", str(carpeta), str(origen)],
        capture_output=True,
        timeout=120,
        env=dict(os.environ, HOME=str(carpeta)),
        check=False,
    )
    destino = carpeta / "origen.xlsx"
    if not destino.is_file():
        raise ValueError(f"no se pudo convertir {ruta.name} para leerlo (¿dañado o con contraseña?)")
    return destino


def _leer_archivo(ruta: Path):
    if ruta.suffix.lower() == ".csv":
        with open(ruta, encoding="utf-8-sig", newline="") as handle:
            texto = handle.read()
        try:
            dialecto = csv.Sniffer().sniff(texto[:4096], delimiters=",;\t|")
        except csv.Error:
            dialecto = csv.excel
        filas = [
            [_numero_en_texto(_valor(c)) if c != "" else None for c in fila]
            for fila in csv.reader(texto.splitlines(), dialecto)
        ]
        return [_tabla_desde_filas(ruta.stem, filas)]
    import openpyxl

    if ruta.suffix.lower() in FORMATOS_ANTIGUOS:
        with tempfile.TemporaryDirectory() as carpeta:
            return _leer_archivo(_convertir_a_xlsx(ruta, Path(carpeta)))
    libro = openpyxl.load_workbook(str(ruta), data_only=True, read_only=True)
    try:
        return [
            _tabla_desde_filas(hoja.title, _filas_hoja(hoja))
            for hoja in libro.worksheets
            if hoja.sheet_state == "visible"
        ]
    finally:
        libro.close()


def _perfil_columna(nombre, valores):
    presentes = [v for v in valores if v is not None]
    perfil = {"columna": nombre, "vacios": len(valores) - len(presentes)}
    if not presentes:
        return perfil
    numeros = [v for v in presentes if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numeros) == len(presentes):
        perfil.update(tipo="numero", suma=round(sum(numeros), 2), min=min(numeros), max=max(numeros))
        return perfil
    textos = [str(v) for v in presentes]
    distintos = {}
    for t in textos:
        distintos[t] = distintos.get(t, 0) + 1
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) for t in textos):
        perfil.update(tipo="fecha", min=min(textos), max=max(textos))
    elif len(distintos) <= 40:
        perfil.update(tipo="categoria", valores=dict(sorted(distintos.items(), key=lambda par: -par[1])))
    else:
        perfil.update(tipo="texto", distintos=len(distintos), ejemplo=textos[0][:40])
    return perfil


def _resumen_tabla(tabla):
    columnas = [_perfil_columna(c, [r.get(c) for r in tabla["registros"]]) for c in tabla["columnas"]]
    return {
        "hoja": tabla["nombre"],
        "filas": tabla["filas"],
        "notas": tabla["notas"],
        "columnas": columnas,
        "muestra": tabla["registros"][:2],
    }


def _archivos_candidatos(workspace: Path, horas: float = 8) -> list[Path]:
    limite = time.time() - horas * 3600
    encontrados = []
    for carpeta in (workspace / "sharepoint_downloads", workspace):
        if carpeta.is_dir():
            encontrados += sorted(
                p
                for p in carpeta.iterdir()
                if p.is_file()
                and p.suffix.lower() in EXTENSIONES_DATOS
                and not p.name.startswith("~$")
                and p.stat().st_mtime >= limite
            )
    return encontrados


def _resolver_entrada(valor: str, workspace: Path) -> Path | None:
    ruta = Path(os.path.expanduser(str(valor)))
    if not ruta.is_absolute():
        ruta = workspace / ruta
    if ruta.is_file():
        return ruta
    coincidencias = [p for p in _archivos_candidatos(workspace, horas=24 * 365) if str(valor).lower() in p.name.lower()]
    return coincidencias[0] if coincidencias else None


def _ver_hoja(carpeta: Path, hoja: str, desde: int, limite: int, perfil: bool = False):
    destino = carpeta / "datos.json"
    if not destino.is_file():
        return {
            "success": False,
            "error": f"No existe {destino.name} en work/{carpeta.name}: llama primero sin 'hoja'.",
        }
    datos = json.loads(destino.read_text(encoding="utf-8"))
    for archivo in datos["archivos"]:
        for tabla in archivo["hojas"]:
            if (
                hoja.lower() in tabla["nombre"].lower()
                or hoja.lower() in archivo["archivo"].lower() + "/" + tabla["nombre"].lower()
            ):
                if perfil:
                    completo = _resumen_tabla(tabla)
                    completo["muestra"] = completo["muestra"][:1]
                    return {"success": True, "archivo": archivo["archivo"], **completo}
                filas = tabla["registros"][desde : desde + min(limite, MAX_FILAS_VER)]
                return {
                    "success": True,
                    "archivo": archivo["archivo"],
                    "hoja": tabla["nombre"],
                    "desde": desde,
                    "total": tabla["filas"],
                    "registros": filas,
                }
    return {"success": False, "error": f"No hay una hoja que contenga '{hoja}'."}


def _extraer_datos(args, **_kwargs):
    workspace = _workspace()
    tema = _slug(args.get("tema") or "datos")
    carpeta = workspace / "work" / tema
    if args.get("hoja"):
        return json.dumps(
            _ver_hoja(
                carpeta,
                str(args["hoja"]),
                int(args.get("desde") or 0),
                int(args.get("limite") or 30),
                bool(args.get("perfil")),
            ),
            ensure_ascii=False,
            default=str,
        )
    pedidos = args.get("archivos") or []
    if isinstance(pedidos, str):
        pedidos = [pedidos]
    rutas = []
    for pedido in pedidos:
        ruta = _resolver_entrada(pedido, workspace)
        if ruta is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"No encuentro el archivo '{pedido}'. Usa hq_datos_extraer sin 'archivos' para tomar los de sharepoint_downloads.",
                },
                ensure_ascii=False,
            )
        rutas.append(ruta)
    if not rutas:
        rutas = _archivos_candidatos(workspace)
    if not rutas:
        return json.dumps(
            {
                "success": False,
                "error": "No hay archivos Excel/CSV de las últimas 8 horas; indica sus nombres en 'archivos'.",
            },
            ensure_ascii=False,
        )
    vistos, archivos, duplicados = {}, [], []
    for ruta in rutas:
        huella = _hash_archivo(ruta)
        nombre = re.sub(r"^[0-9a-f]{8}_", "", ruta.name)
        if huella in vistos:
            duplicados.append(nombre)
            continue
        vistos[huella] = nombre
        try:
            hojas = _leer_archivo(ruta)
        except Exception as exc:
            return json.dumps({"success": False, "error": f"No pude leer {nombre}: {exc}"}, ensure_ascii=False)
        archivos.append(
            {
                "archivo": nombre,
                "origen": str(ruta.relative_to(workspace)) if workspace in ruta.parents else str(ruta),
                "hojas": hojas,
            }
        )
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / "datos.json"
    destino.write_text(json.dumps({"archivos": archivos}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    resumen = {
        "success": True,
        "guardado": str(destino.relative_to(workspace)),
        "duplicados_ignorados": duplicados,
        "archivos": [],
    }
    for archivo in archivos:
        resumen["archivos"].append(
            {"archivo": archivo["archivo"], "hojas": [_resumen_tabla(t) for t in archivo["hojas"]]}
        )
    texto = json.dumps(resumen, ensure_ascii=False, default=str)
    if len(texto) > MAX_RESUMEN:
        for archivo in resumen["archivos"]:
            for hoja in archivo["hojas"]:
                hoja["muestra"] = []
        texto = json.dumps(resumen, ensure_ascii=False, default=str)
    if len(texto) > MAX_RESUMEN:
        for archivo in resumen["archivos"]:
            for hoja in archivo["hojas"]:
                utiles, otras = [], []
                for c in hoja["columnas"]:
                    if c.get("tipo") == "categoria":
                        utiles.append({"columna": c["columna"], "valores": dict(list(c["valores"].items())[:8])})
                    elif c.get("tipo") == "numero":
                        utiles.append({"columna": c["columna"], "suma": c["suma"]})
                    else:
                        otras.append(c["columna"])
                hoja["columnas"] = utiles
                hoja["otras_columnas"] = otras
        resumen["nota"] = "Resumen reducido: usa hoja='...' con perfil=true para ver todas las columnas de una hoja."
        texto = json.dumps(resumen, ensure_ascii=False, default=str)
    if len(texto) > MAX_RESUMEN:
        for archivo in resumen["archivos"]:
            for hoja in archivo["hojas"]:
                hoja["columnas"] = [c["columna"] for c in hoja["columnas"]]
                hoja.pop("otras_columnas", None)
        texto = json.dumps(resumen, ensure_ascii=False, default=str)
    return texto


DESC_DATOS = (
    "Lee archivos Excel/CSV ya descargados y guarda TODOS sus datos en work/<tema>/datos.json en UNA llamada, sin que escribas código. "
    "Sin 'archivos' toma los Excel/CSV de las últimas 8 horas en sharepoint_downloads (ignora duplicados). Devuelve por hoja: filas, columnas, conteos por categoría, "
    "sumas y fechas mín/máx. Úsala ANTES de analizar o reportar datos de Excel; no uses execute_code ni vuelvas a abrir los originales. "
    "Con 'hoja' (y 'desde', 'limite') devuelve filas concretas del datos.json ya guardado."
)


PARAMS_DATOS = {
    "archivos": {"type": "array", "items": {"type": "string"}, "description": "Rutas o parte del nombre; opcional."},
    "tema": {"type": "string", "description": "Carpeta work/<tema>/ donde se guarda datos.json (ej. licencias)."},
    "hoja": {
        "type": "string",
        "description": "Parte del nombre de una hoja: devuelve sus filas del datos.json guardado.",
    },
    "perfil": {
        "type": "boolean",
        "description": "Con 'hoja': devuelve el perfil completo de columnas (valores distintos, sumas, fechas).",
    },
    "desde": {"type": "integer", "description": "Con 'hoja': fila inicial (0)."},
    "limite": {"type": "integer", "description": "Con 'hoja': máximo de filas (hasta 60)."},
}


MAX_SALIDA_CONSULTA = 9000


MAX_CONSULTAS = 6


def _sin_acentos(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor if valor is not None else "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto.lower()).strip()


def _columna_real(nombre, columnas):
    if nombre in columnas:
        return nombre
    buscada = _sin_acentos(nombre)
    exactas = [c for c in columnas if _sin_acentos(c) == buscada]
    if len(exactas) == 1:
        return exactas[0]
    parecidas = [c for c in columnas if buscada and buscada in _sin_acentos(c)]
    if len(parecidas) == 1:
        return parecidas[0]
    raise ValueError(f"no encuentro la columna '{nombre}'. Columnas: {columnas}")


def _numero(valor):
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).replace(",", ".")) if re.fullmatch(r"\s*-?\d+([.,]\d+)?\s*", str(valor)) else None
    except ValueError:
        return None


def _cumple(fila, filtro, columnas):
    columna = _columna_real(filtro.get("columna", ""), columnas)
    operador = _sin_acentos(filtro.get("op") or "=")
    valor = filtro.get("valor")
    celda = fila.get(columna)
    if operador in ("vacio", "no_vacio"):
        vacio = celda in (None, "")
        return vacio if operador == "vacio" else not vacio
    if operador in ("anio", "mes"):
        coincidencia = re.match(r"(\d{4})-(\d{2})", str(celda or ""))
        if not coincidencia:
            return False
        return int(coincidencia.group(1 if operador == "anio" else 2)) == int(valor)
    if operador in ("=", "==", "igual"):
        return _sin_acentos(celda) == _sin_acentos(valor)
    if operador in ("!=", "distinto"):
        return _sin_acentos(celda) != _sin_acentos(valor)
    if operador in ("contiene", "no_contiene"):
        dentro = _sin_acentos(valor) in _sin_acentos(celda)
        return dentro if operador == "contiene" else not dentro
    if operador == "en":
        return _sin_acentos(celda) in {_sin_acentos(v) for v in (valor if isinstance(valor, list) else [valor])}
    if operador == "entre":
        minimo, maximo = valor
        return _comparar(celda, minimo, ">=") and _comparar(celda, maximo, "<=")
    if operador in (">", ">=", "<", "<="):
        return _comparar(celda, valor, operador)
    raise ValueError(
        f"operador '{operador}' no válido. Usa: =, !=, contiene, no_contiene, en, >, >=, <, <=, entre, vacio, no_vacio, anio, mes"
    )


def _comparar(celda, valor, operador) -> bool:
    if celda in (None, ""):
        return False
    a, b = _numero(celda), _numero(valor)
    if a is None or b is None:
        a, b = str(celda), str(valor)
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[operador]


def _recortar(valor, largo=110):
    texto = str(valor) if valor is not None else ""
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def _tabla_guardada(carpeta: Path, hoja: str):
    destino = carpeta / "datos.json"
    if not destino.is_file():
        raise ValueError(f"No existe {destino.name} en work/{carpeta.name}: ejecuta primero hq_datos_extraer.")
    datos = json.loads(destino.read_text(encoding="utf-8"))
    buscada = _sin_acentos(hoja)
    for archivo in datos["archivos"]:
        for tabla in archivo["hojas"]:
            if buscada in _sin_acentos(tabla["nombre"]) or buscada in _sin_acentos(
                archivo["archivo"] + "/" + tabla["nombre"]
            ):
                return archivo["archivo"], tabla
    nombres = [t["nombre"] for a in datos["archivos"] for t in a["hojas"]]
    raise ValueError(f"No hay una hoja que contenga '{hoja}'. Hojas: {nombres}")


def _consultar_datos(args, **_kwargs):
    consultas = args.get("consultas")
    if isinstance(consultas, list) and consultas:
        base = {k: args[k] for k in ("tema", "hoja") if args.get(k)}
        resultados = []
        for consulta in consultas[:MAX_CONSULTAS]:
            if not isinstance(consulta, dict):
                resultados.append({"success": False, "error": "cada consulta debe ser un objeto"})
                continue
            resultados.append(json.loads(_consultar_datos({**base, **consulta})))
        return json.dumps({"success": True, "resultados": resultados}, ensure_ascii=False, default=str)
    try:
        carpeta = _workspace() / "work" / _slug(args.get("tema") or "datos")
        if not args.get("hoja"):
            raise ValueError("indica 'hoja' (parte del nombre de la hoja)")
        archivo, tabla = _tabla_guardada(carpeta, str(args["hoja"]))
        columnas = tabla["columnas"]
        filtros = args.get("filtros") or []
        if isinstance(filtros, dict):
            filtros = [filtros]
        filas = [f for f in tabla["registros"] if all(_cumple(f, filtro, columnas) for filtro in filtros)]
        resultado = {
            "success": True,
            "archivo": archivo,
            "hoja": tabla["nombre"],
            "filas_en_la_hoja": tabla["filas"],
            "coinciden": len(filas),
        }
        agrupar = args.get("agrupar_por") or []
        agrupar = [agrupar] if isinstance(agrupar, str) else agrupar
        sumar = _columna_real(args["sumar"], columnas) if args.get("sumar") else None
        if agrupar:
            claves = [_columna_real(c, columnas) for c in agrupar]
            grupos = {}
            for fila in filas:
                clave = tuple(_recortar(fila.get(c), 60) for c in claves)
                grupo = grupos.setdefault(clave, {"conteo": 0, "suma": 0.0})
                grupo["conteo"] += 1
                if sumar and _numero(fila.get(sumar)) is not None:
                    grupo["suma"] += _numero(fila.get(sumar))
            ordenados = sorted(grupos.items(), key=lambda par: -par[1]["conteo"])
            resultado["grupos"] = [
                {
                    **dict(zip(claves, clave, strict=True)),
                    "conteo": g["conteo"],
                    **({"suma": round(g["suma"], 2)} if sumar else {}),
                }
                for clave, g in ordenados[:MAX_FILAS_VER]
            ]
            resultado["total_grupos"] = len(grupos)
        else:
            if sumar:
                resultado["suma"] = round(sum(_numero(f.get(sumar)) or 0 for f in filas), 2)
            orden = args.get("ordenar")
            if orden:
                descendente = str(orden).startswith("-")
                columna = _columna_real(str(orden).lstrip("-"), columnas)
                filas = sorted(
                    filas, key=lambda f: (f.get(columna) is None, str(f.get(columna) or "")), reverse=descendente
                )
            elegidas = [_columna_real(c, columnas) for c in args.get("columnas") or []]
            if not elegidas:
                elegidas = [c for c in columnas if any(f.get(c) not in (None, "") for f in filas[:20])][:8]
            desde = int(args.get("desde") or 0)
            limite = min(int(args.get("limite") or 30), MAX_FILAS_VER)
            seleccion = filas[desde : desde + limite]
            resultado["desde"] = desde
            resultado["filas"] = [{c: _recortar(f.get(c)) for c in elegidas} for f in seleccion]
        texto = json.dumps(resultado, ensure_ascii=False, default=str)
        while len(texto) > MAX_SALIDA_CONSULTA and resultado.get("filas"):
            resultado["filas"] = resultado["filas"][: max(1, len(resultado["filas"]) // 2)]
            resultado["nota"] = (
                "Salida recortada: usa 'desde' y 'limite' para ver más filas, o 'columnas' para menos columnas."
            )
            texto = json.dumps(resultado, ensure_ascii=False, default=str)
        return texto
    except (ValueError, TypeError, KeyError) as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


DESC_CONSULTA = (
    "Consulta las hojas ya guardadas por hq_datos_extraer (work/<tema>/datos.json) SIN escribir código: filtra, ordena, "
    "cuenta o suma por grupos y lista filas concretas. Úsala para responder preguntas sobre los datos "
    "('¿cuáles son los 132 casos de 2026?', '¿cuántos por sucursal?', '¿quién tiene licencia asignada en MEJ?'). "
    "No uses execute_code para esto."
)


PARAMS_CONSULTA = {
    "tema": {"type": "string", "description": "Carpeta work/<tema>/ del datos.json (el mismo de hq_datos_extraer)."},
    "hoja": {"type": "string", "description": "Parte del nombre de la hoja."},
    "consultas": {
        "type": "array",
        "items": {"type": "object"},
        "description": "Varias consultas en UNA llamada (hasta 6): cada una con los mismos campos de abajo; heredan tema y hoja.",
    },
    "filtros": {
        "type": "array",
        "items": {"type": "object"},
        "description": "[{columna, op, valor}]; op: =, !=, contiene, no_contiene, en (lista), >, >=, <, <=, entre ([a,b]), vacio, no_vacio, anio, mes. Todos deben cumplirse.",
    },
    "columnas": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Columnas a mostrar (por defecto las primeras con datos).",
    },
    "agrupar_por": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Columnas por las que contar (devuelve conteo por grupo).",
    },
    "sumar": {"type": "string", "description": "Columna numérica a sumar (total o por grupo)."},
    "ordenar": {"type": "string", "description": "Columna; con '-' delante, descendente."},
    "desde": {"type": "integer", "description": "Fila inicial (0)."},
    "limite": {"type": "integer", "description": "Máximo de filas (hasta 60)."},
}


def _fabrica():
    ruta = Path(__file__).with_name("fabrica_skills.py")
    spec = importlib.util.spec_from_file_location("hq_fabrica_skills", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _casa_hermes() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001  # fuera del runtime se usa la variable de entorno
        return Path(os.environ.get("HERMES_HOME") or (_workspace() / ".hermes"))


def _compartir_skill(fab, accion, args, raiz, existentes):
    nombre = str(args.get("nombre") or "").strip()
    autor = (os.environ.get("HERMESHQ_AGENT_ID") or "agente")[:8]
    if accion == "retirar_compartida":
        dueno = fab.autor_de(nombre)
        if dueno is None:
            return json.dumps(
                {"success": False, "error": f"No hay una skill compartida llamada '{nombre}'."}, ensure_ascii=False
            )
        if dueno != autor:
            return json.dumps(
                {"success": False, "error": "Solo quien la compartió puede retirarla; pídelo al administrador."},
                ensure_ascii=False,
            )
        fab.retirar_compartida(nombre)
        return json.dumps(
            {"success": True, "mensaje": f"'{nombre}' ya no está en la biblioteca compartida."}, ensure_ascii=False
        )
    if nombre not in existentes:
        return json.dumps(
            {"success": False, "error": f"Solo se comparten tus propias skills. Las tuyas: {sorted(existentes)}"},
            ensure_ascii=False,
        )
    origen = raiz / existentes[nombre]["categoria"] / nombre
    personales = fab.datos_personales_en_carpeta(origen)
    if personales:
        detalle = "; ".join(f"{tipo} (en {', '.join(archivos)})" for tipo, archivos in personales.items())
        return json.dumps(
            {
                "success": False,
                "error": f"Para que otra persona pueda usarla no debe llevar datos personales: {detalle}. Cámbialos por un paso que se los pida al usuario en cada uso y vuelve a intentar.",
            },
            ensure_ascii=False,
        )
    if not args.get("aprobado_por_el_usuario"):
        return json.dumps(
            {
                "success": False,
                "error": "Falta la aprobación: cuéntale al usuario que todos los agentes podrán usar su skill (las que llevan scripts las revisa antes el administrador) y, con su sí, vuelve a llamar con aprobado_por_el_usuario=true.",
            },
            ensure_ascii=False,
        )
    resultado = fab.compartir(nombre, origen, autor)
    if resultado["estado"] == "existe":
        return json.dumps(
            {"success": False, "error": f"Ya hay una skill compartida llamada '{nombre}'. Cambia el nombre."},
            ensure_ascii=False,
        )
    mensaje = (
        "Quedó pendiente de revisión del administrador porque lleva scripts; cuando la apruebe estará disponible para todos."
        if resultado["estado"] == "pendiente"
        else "Quedó disponible para todos los agentes desde ya (aparece como biblioteca/" + nombre + ")."
    )
    return json.dumps({"success": True, "estado": resultado["estado"], "mensaje": mensaje}, ensure_ascii=False)


def _ajustar_skill_administrada(fab, args, raiz):
    base = str(args.get("base") or "").strip()
    administradas = fab.skills_administradas(_skills_root())
    if base not in administradas:
        return json.dumps(
            {"success": False, "error": f"'base' debe ser una skill administrada: {sorted(administradas)}"},
            ensure_ascii=False,
        )
    reglas = [str(r).strip() for r in (args.get("reglas") or []) if str(r).strip()]
    if not 1 <= len(reglas) <= 16 or any(len(r) > 300 or "```" in r or "<html" in r.lower() for r in reglas):
        return json.dumps(
            {
                "success": False,
                "error": "'reglas' debe tener de 1 a 16 reglas cortas (máx. 300 caracteres), sin código ni HTML.",
            },
            ensure_ascii=False,
        )
    if not args.get("aprobado_por_el_usuario"):
        return json.dumps(
            {
                "success": False,
                "error": "Falta la aprobación: muestra las reglas al usuario y, con su sí, vuelve a llamar con aprobado_por_el_usuario=true.",
            },
            ensure_ascii=False,
        )
    destino = raiz / "usuario" / f"{base}-ajustes"
    respaldo = None
    if destino.exists():
        carpeta = raiz / ".respaldos"
        carpeta.mkdir(parents=True, exist_ok=True)
        respaldo = carpeta / f"{base}-ajustes-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
        shutil.copy2(destino / "SKILL.md", respaldo)
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "SKILL.md").write_text(fab.componer_ajuste(base, administradas[base], reglas), encoding="utf-8")
    return json.dumps(
        {
            "success": True,
            "ruta": str(destino / "SKILL.md"),
            "respaldo": str(respaldo) if respaldo else None,
            "siguiente": f"Quedó guardado. Desde ahora las herramientas de {base} te recuerdan estas reglas en cada uso; para cambiarlas vuelve a llamar con las reglas completas.",
        },
        ensure_ascii=False,
    )


def _skill_crear(args, **_kwargs):
    fab = _fabrica()
    accion = str(args.get("accion") or "crear").strip().lower()
    raiz = fab.carpeta_skills(_casa_hermes())
    existentes = fab.skills_de_usuario(raiz)
    if accion == "guia":
        return json.dumps({"success": True, "guia": fab.GUIA}, ensure_ascii=False)
    if accion == "biblioteca":
        return json.dumps({"success": True, **fab.listar_biblioteca()}, ensure_ascii=False)
    if accion in ("compartir", "retirar_compartida"):
        return _compartir_skill(fab, accion, args, raiz, existentes)
    if accion == "listar":
        return json.dumps({"success": True, "skills": existentes}, ensure_ascii=False)
    if accion == "eliminar":
        nombre = str(args.get("nombre") or "").strip()
        destino = fab.retirar(nombre, raiz)
        if destino is None:
            return json.dumps(
                {"success": False, "error": f"No encuentro tu skill '{nombre}'. Tus skills: {sorted(existentes)}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": True, "mensaje": f"Skill '{nombre}' retirada (se puede recuperar)."}, ensure_ascii=False
        )
    if accion == "ajustar":
        return _ajustar_skill_administrada(fab, args, raiz)
    skill = {
        k: args.get(k)
        for k in (
            "nombre",
            "descripcion",
            "frases",
            "pasos",
            "reglas",
            "entrega",
            "ejemplo",
            "no_usar",
            "reemplazar",
            "scripts",
        )
    }
    herramientas = fab.herramientas_disponibles(_skills_root().parent if _skills_root() else None)
    administradas = fab.skills_administradas(_skills_root())
    errores, avisos = fab.validar(skill, set(existentes), administradas, herramientas)
    if accion == "validar" or errores:
        return json.dumps(
            {
                "success": not errores,
                "errores": errores,
                "avisos": avisos,
                "consejos": fab.consejos(skill, errores, avisos),
                "vista_previa": fab.componer(skill)[:1800] if not errores else None,
                "siguiente": "Corrige los errores y vuelve a validar."
                if errores
                else "Muéstrale el plan al usuario y, con su sí, llama accion='crear' con aprobado_por_el_usuario=true.",
            },
            ensure_ascii=False,
        )
    if accion != "crear":
        return json.dumps(
            {"success": False, "error": "accion debe ser validar, crear, listar o eliminar"}, ensure_ascii=False
        )
    if not args.get("aprobado_por_el_usuario"):
        return json.dumps(
            {
                "success": False,
                "error": "Falta la aprobación: muestra al usuario el nombre, qué hace y los pasos, y cuando responda que sí vuelve a llamar con aprobado_por_el_usuario=true.",
                "avisos": avisos,
            },
            ensure_ascii=False,
        )
    destino, respaldo = fab.guardar(skill, raiz, _workspace(), args.get("categoria") or "usuario")
    return json.dumps(
        {
            "success": True,
            "nombre": skill["nombre"],
            "ruta": str(destino / "SKILL.md"),
            "respaldo": str(respaldo) if respaldo else None,
            "avisos": avisos,
            "consejos": fab.consejos(skill, errores, avisos),
            "siguiente": "Dile al usuario que quedó creada y pídele que la pruebe pegando su ejemplo en un mensaje nuevo; si algo no sale como espera, la ajustas con reemplazar=true. Explícale en dos líneas por qué quedó bien y ofrécele compartirla con los demás (accion='compartir').",
        },
        ensure_ascii=False,
    )


DESC_SKILL_CREAR = (
    "Forma validada de crear, modificar, listar o eliminar skills del usuario. Rellenas campos "
    "cortos y la herramienta compone la skill, la VALIDA y la guarda: combina herramientas que ya existen y puede "
    "adjuntar scripts de Python propios (validados). Con accion='ajustar' el usuario agrega reglas personales a una skill "
    "administrada sin tocar la original. accion='guia' devuelve las buenas prácticas para enseñarle al usuario a hacerlas bien; 'compartir' publica una skill propia para los demás (sin datos personales; con scripts la revisa el administrador); 'biblioteca' lista lo compartido. Flujo: accion='validar' con los campos; muestra al usuario el plan y espera su sí; "
    "accion='crear' con aprobado_por_el_usuario=true. Si lo que pide el usuario no lo cubre ninguna herramienta, no crees "
    "una skill: pídele la capacidad al administrador. Para cambiar una skill propia usa reemplazar=true. No uses nombres de skills administradas."
)


EXTENSIONES_HTML = (".html", ".htm")


CARPETAS_COMPARAR = ("uploads", "sharepoint_downloads", "outputs", "work")


def _comparador():
    ruta = Path(__file__).with_name("comparar.py")
    spec = importlib.util.spec_from_file_location("hq_comparar", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _buscar_para_comparar(valor, workspace: Path):
    ruta = Path(os.path.expanduser(str(valor)))
    if not ruta.is_absolute():
        ruta = workspace / ruta
    if ruta.is_file():
        return ruta, []
    pedazo = str(valor).lower().strip()
    permitidas = EXTENSIONES_HTML + EXTENSIONES_DATOS
    candidatos = []
    for carpeta in [workspace / c for c in CARPETAS_COMPARAR] + [workspace]:
        if not carpeta.is_dir():
            continue
        patron = carpeta.rglob("*") if carpeta.name in CARPETAS_COMPARAR else carpeta.glob("*")
        for p in patron:
            if (
                p.is_file()
                and p.suffix.lower() in permitidas
                and not p.name.startswith("~$")
                and ".hermes" not in p.parts
            ):
                if pedazo in p.name.lower() and p not in candidatos:
                    candidatos.append(p)
    candidatos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    exactos = [p for p in candidatos if p.name.lower() == pedazo or p.stem.lower() == pedazo]
    if len(exactos) == 1 or (exactos and len({p.stat().st_size for p in exactos}) == 1):
        return exactos[0], []
    if len(candidatos) == 1:
        return candidatos[0], []
    return None, [str(p.relative_to(workspace)) if p.is_relative_to(workspace) else str(p) for p in candidatos[:8]]


def _elegir_hoja(tablas, pedazo):
    if pedazo:
        for t in tablas:
            if _sin_acentos(pedazo) in _sin_acentos(t["nombre"]):
                return t
        return None
    return tablas[0] if tablas else None


def _comparar_versiones(args, **_kwargs):
    def error(texto, **extra):
        return json.dumps({"success": False, "error": texto, **extra}, ensure_ascii=False)

    if not args.get("antes") or not args.get("despues"):
        return error("Indica 'antes' y 'despues': el nombre (o parte del nombre) de cada archivo, HTML o Excel/CSV.")
    workspace = _workspace()
    rutas = []
    for clave in ("antes", "despues"):
        ruta, candidatos = _buscar_para_comparar(args[clave], workspace)
        if ruta is None:
            if candidatos:
                return error(
                    f"'{args[clave]}' coincide con varios archivos; usa el nombre completo.", candidatos=candidatos
                )
            return error(f"No encontré '{args[clave]}' en uploads, sharepoint_downloads, outputs ni work.")
        rutas.append(ruta)
    ext = {r.suffix.lower() for r in rutas}
    es_html = all(e in EXTENSIONES_HTML for e in ext)
    es_datos = all(e in EXTENSIONES_DATOS for e in ext)
    if not (es_html or es_datos):
        return error("Los dos archivos deben ser del mismo tipo: HTML, o Excel/CSV.")
    comp = _comparador()
    base = {"success": True, "antes": rutas[0].name, "despues": rutas[1].name}
    try:
        if es_html:
            textos = [r.read_text(encoding="utf-8", errors="replace") for r in rutas]
            resultado = comp.comparar_html(*textos)
            resultado["tipo"] = "html"
        else:
            hoja_a = args.get("hoja_antes") or args.get("hoja")
            hoja_b = args.get("hoja_despues") or args.get("hoja")
            tablas = [_leer_archivo(r) for r in rutas]
            elegidas = [_elegir_hoja(tablas[0], hoja_a), _elegir_hoja(tablas[1], hoja_b)]
            if not (hoja_a or hoja_b) and len(tablas[0]) > 1 and len(tablas[1]) > 1:
                comunes = [
                    t0["nombre"]
                    for t0 in tablas[0]
                    if any(_sin_acentos(t0["nombre"]) == _sin_acentos(t1["nombre"]) for t1 in tablas[1])
                ]
                if comunes:
                    elegidas = [_elegir_hoja(tablas[0], comunes[0]), _elegir_hoja(tablas[1], comunes[0])]
                    base["otras_hojas_comunes"] = comunes[1:8]
            if elegidas[0] is None or elegidas[1] is None:
                nombres = [[t["nombre"] for t in lista] for lista in tablas]
                return error(
                    "No encontré esa hoja en uno de los archivos.", hojas_antes=nombres[0], hojas_despues=nombres[1]
                )
            resultado = comp.comparar_tablas(elegidas[0], elegidas[1], args.get("clave"))
            resultado["tipo"] = "datos"
            resultado["hoja_antes"], resultado["hoja_despues"] = elegidas[0]["nombre"], elegidas[1]["nombre"]
            if resultado.get("error"):
                return error(resultado["error"], columnas_antes=elegidas[0]["columnas"][:20])
    except subprocess.TimeoutExpired:
        return error("La comparación tardó demasiado.")
    except Exception as exc:  # noqa: BLE001  # un archivo ilegible no debe tumbar la tarea
        return error(f"No pude leer uno de los archivos: {str(exc)[:200]}")
    base.update(resultado)
    base["siguiente"] = (
        "Cita las cifras de 'resumen' y los ejemplos tal cual; no recalcules con código. Si piden un HTML con la "
        "comparación, preséntalas como una lista ordenada."
    )
    return json.dumps(base, ensure_ascii=False)


DESC_COMPARAR = (
    "Compara DOS versiones de un archivo SIN escribir código y devuelve exactamente qué cambió. Dos HTML (informes, "
    "páginas): secciones nuevas, quitadas o modificadas con sus frases, estilo y estructura. Dos Excel/CSV: filas "
    "agregadas, quitadas y modificadas (con 'clave' ves qué valor cambió en cada fila), columnas nuevas y totales. "
    "Úsala para «qué cambió entre V2 y V3», «compara estos dos Excel», «resume la modificación». No uses execute_code."
)


PARAMS_COMPARAR = {
    "antes": {"type": "string", "description": "Versión anterior: nombre o parte del nombre del archivo."},
    "despues": {"type": "string", "description": "Versión nueva: nombre o parte del nombre del archivo."},
    "hoja": {
        "type": "string",
        "description": "Solo Excel: parte del nombre de la hoja (por defecto la primera en común).",
    },
    "clave": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Solo Excel: columna(s) que identifican una fila (ej. ['ID'] o ['Usuario','Licencia']).",
    },
}


def _modulo_exportar():
    ruta = Path(__file__).with_name("exportar.py")
    spec = importlib.util.spec_from_file_location("hq_exportar", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _exportar_datos(args, **_kwargs):
    def error(texto):
        return json.dumps({"success": False, "error": texto}, ensure_ascii=False)

    try:
        exp = _modulo_exportar()
        tema = _slug(args.get("tema") or "datos")
        carpeta = _workspace() / "work" / tema
        if not args.get("hoja"):
            return error("indica 'hoja' (parte del nombre de la hoja)")
        archivo, tabla = _tabla_guardada(carpeta, str(args["hoja"]))
        columnas = tabla["columnas"]
        filtros = args.get("filtros") or []
        filtros = [filtros] if isinstance(filtros, dict) else filtros
        filas = [dict(f) for f in tabla["registros"] if all(_cumple(f, filtro, columnas) for filtro in filtros)]
        descartadas = []
        quitar = args.get("quitar_repetidos")
        if quitar:
            if not isinstance(quitar, dict) or not quitar.get("fecha") or not quitar.get("descripcion"):
                return error("quitar_repetidos necesita {fecha, descripcion, id?, sucursal?} (nombres de columna).")
            filas, descartadas = exp.depurar_repetidos(
                filas,
                col_id=_columna_real(quitar["id"], columnas) if quitar.get("id") else None,
                col_fecha=_columna_real(quitar["fecha"], columnas),
                col_descripcion=_columna_real(quitar["descripcion"], columnas),
                col_grupo=_columna_real(quitar["sucursal"], columnas) if quitar.get("sucursal") else None,
            )
        calculadas = []
        for c in args.get("columnas_calculadas") or []:
            if not isinstance(c, dict) or not c.get("nombre") or not c.get("tipo") or not c.get("de"):
                return error("cada columna calculada necesita {nombre, tipo, de}.")
            calculadas.append({**c, "de": c["de"]})
        reales = {c["de"]: _columna_real(c["de"], columnas) for c in calculadas}
        for fila in filas:
            for c in calculadas:
                fila[c["nombre"]] = exp.valor_calculado(c, fila, reales)
        if len(filas) > exp.MAX_FILAS:
            return error(f"son {len(filas)} filas; el máximo es {exp.MAX_FILAS}. Agrega filtros.")
        orden = args.get("ordenar")
        if orden:
            columna = str(orden).lstrip("-")
            todas = columnas + [c["nombre"] for c in calculadas]
            columna = _columna_real(columna, todas)
            filas.sort(
                key=lambda f: (f.get(columna) is None, str(f.get(columna) or "")), reverse=str(orden).startswith("-")
            )
        elegidas = [_columna_real(c, columnas + [x["nombre"] for x in calculadas]) for c in args.get("columnas") or []]
        if not elegidas:
            elegidas = [c for c in columnas if any(f.get(c) not in (None, "") for f in filas[:50])]
            elegidas += [c["nombre"] for c in calculadas]
        hojas = [(str(args.get("titulo") or tabla["nombre"])[:31], elegidas, filas)]
        columnas_resumen = [
            _columna_real(c, columnas + [x["nombre"] for x in calculadas]) for c in args.get("resumen_por") or []
        ]
        if columnas_resumen:
            filas_resumen = []
            for columna, pares in exp.resumen_filas(filas, columnas_resumen, calculadas):
                filas_resumen += [[columna, valor, cuenta] for valor, cuenta in pares]
                filas_resumen.append(["", "", ""])
            hojas.append(("Resumen", ["Columna", "Valor", "Casos"], filas_resumen))
        if descartadas:
            hojas.append(
                (
                    "Depuración",
                    ["Conservado", "Descartado (repetido)", "Motivo"],
                    [[d["conservado"], d["descartado"], d["motivo"]] for d in descartadas],
                )
            )
        nombre = _slug(args.get("nombre") or f"{tema}-detalle")
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f"{nombre}.xlsx"
        exp.escribir_excel(destino, hojas)
    except (ValueError, TypeError, KeyError) as exc:
        return error(str(exc))
    relativa = str(destino.relative_to(_workspace()))
    resultado = {
        "success": True,
        "path": relativa,
        "adjuntar_en_la_respuesta": [f"MEDIA:{relativa}"],
        "hoja_origen": tabla["nombre"],
        "filas": len(filas),
        "columnas": elegidas,
        "hojas": [h[0] for h in hojas],
        "repetidos_quitados": len(descartadas),
        "siguiente": "Responde con un resumen corto (filas, repetidos quitados con su motivo) y la línea MEDIA. No abras el Excel con código.",
    }
    if descartadas:
        resultado["repetidos_detalle"] = descartadas[:15]
    return json.dumps(resultado, ensure_ascii=False, default=str)


DESC_EXPORTAR = (
    "Genera un EXCEL ordenado (encabezado, filtros, columnas ajustadas) con filas de una hoja ya guardada por "
    "hq_datos_extraer, SIN escribir código: filtra, agrega columnas calculadas (año, mes, trimestre, o una categoría "
    "según el valor de otra columna), quita repetidos y agrega hojas Resumen y Depuración. Úsala para «dame el detalle "
    "de estos casos en Excel con año, mes y categoría». No uses execute_code ni openpyxl para esto."
)


PARAMS_EXPORTAR = {
    "tema": {"type": "string", "description": "Carpeta work/<tema>/ del datos.json (el mismo de hq_datos_extraer)."},
    "hoja": {"type": "string", "description": "Parte del nombre de la hoja."},
    "nombre": {"type": "string", "description": "Nombre del archivo sin extensión."},
    "titulo": {"type": "string", "description": "Nombre de la hoja principal del Excel."},
    "filtros": {
        "type": "array",
        "items": {"type": "object"},
        "description": "[{columna, op, valor}] como en hq_datos_consultar (op: =, !=, contiene, en, anio, mes, entre, …).",
    },
    "columnas": {"type": "array", "items": {"type": "string"}, "description": "Columnas a incluir, en orden."},
    "columnas_calculadas": {
        "type": "array",
        "items": {"type": "object"},
        "description": (
            "[{nombre, tipo, de}] tipo: anio | mes | mes_nombre | trimestre (de = columna de fecha) o clasificar "
            "(de = columna; reglas: {'Accidente': ['STP','CTP'], …}; otro: 'Incidente')."
        ),
    },
    "ordenar": {"type": "string", "description": "Columna; con '-' delante, descendente."},
    "resumen_por": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Columnas (incluidas las calculadas) para una hoja Resumen con conteos.",
    },
    "quitar_repetidos": {
        "type": "object",
        "description": (
            "{fecha, descripcion, id?, sucursal?}: nombres de columna. Quita el mismo caso registrado dos veces "
            "(mismo día y descripción casi idéntica, o N° consecutivos con texto idéntico); se conserva el N° menor y "
            "se agrega la hoja Depuración con cada par."
        ),
    },
}


def _check_requirements():
    try:
        import docx  # noqa: F401
        import pptx  # noqa: F401

        return True
    except ImportError:
        return False


def register(ctx):
    ctx.register_tool(
        name="hq_datos_extraer",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_datos_extraer",
            "description": DESC_DATOS,
            "parameters": {"type": "object", "properties": PARAMS_DATOS, "required": []},
        },
        handler=_extraer_datos,
        check_fn=_check_requirements,
        description=DESC_DATOS,
        emoji="🗂️",
    )
    ctx.register_tool(
        name="hq_datos_consultar",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_datos_consultar",
            "description": DESC_CONSULTA,
            "parameters": {"type": "object", "properties": PARAMS_CONSULTA, "required": ["hoja"]},
        },
        handler=_consultar_datos,
        check_fn=_check_requirements,
        description=DESC_CONSULTA,
        emoji="🔎",
    )
    ctx.register_tool(
        name="hq_datos_exportar",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_datos_exportar",
            "description": DESC_EXPORTAR,
            "parameters": {"type": "object", "properties": PARAMS_EXPORTAR, "required": ["hoja"]},
        },
        handler=_exportar_datos,
        check_fn=_check_requirements,
        description=DESC_EXPORTAR,
        emoji="📤",
    )
    ctx.register_tool(
        name="hq_comparar",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_comparar",
            "description": DESC_COMPARAR,
            "parameters": {"type": "object", "properties": PARAMS_COMPARAR, "required": ["antes", "despues"]},
        },
        handler=_comparar_versiones,
        check_fn=_check_requirements,
        description=DESC_COMPARAR,
        emoji="🔍",
    )
    ctx.register_tool(
        name="hq_skill_crear",
        toolset="hermeshq_documentos",
        schema={
            "name": "hq_skill_crear",
            "description": DESC_SKILL_CREAR,
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": [
                            "validar",
                            "crear",
                            "listar",
                            "eliminar",
                            "ajustar",
                            "guia",
                            "compartir",
                            "retirar_compartida",
                            "biblioteca",
                        ],
                        "description": "Por defecto crear (valida antes).",
                    },
                    "nombre": {"type": "string", "description": "minúsculas con guiones, 3-40 caracteres"},
                    "descripcion": {"type": "string", "description": "Qué hace, en una frase (30-240 caracteres)."},
                    "frases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 o más formas en que el usuario lo pediría, con sus palabras.",
                    },
                    "pasos": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "2-10 pasos: [{texto, herramienta?}] con herramientas que ya existen.",
                    },
                    "reglas": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Hasta 8 reglas propias (qué confirmar, qué nunca hacer).",
                    },
                    "entrega": {"type": "string", "description": "Qué se entrega y cómo (archivo, resumen, correo…)."},
                    "ejemplo": {"type": "string", "description": "Una frase real con la que el usuario lo pediría."},
                    "no_usar": {"type": "string", "description": "Opcional: para qué NO sirve."},
                    "reemplazar": {
                        "type": "boolean",
                        "description": "true para modificar una skill propia ya existente (se respalda).",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true solo si el usuario ya dijo que sí al plan.",
                    },
                },
                "required": [],
            },
        },
        handler=_skill_crear,
        check_fn=_check_requirements,
        description=DESC_SKILL_CREAR,
        emoji="🧩",
    )
