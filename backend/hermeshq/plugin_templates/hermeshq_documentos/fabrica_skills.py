"""Fábrica de skills de usuario: compone la skill desde campos, la valida y la guarda.

Una skill de usuario solo combina herramientas que YA existen. No lleva scripts, HTML ni datos inventados.
"""

import ast
import datetime
import difflib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

NOMBRE_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
MAX_CHARS = 6000
DESC_MIN, DESC_MAX = 30, 240
MAX_PASOS, MAX_REGLAS = 10, 8
PREFIJOS = ("ms365", "sharepoint", "hq", "teams", "onedrive")
TOKEN_HERRAMIENTA = re.compile(r"\b((?:" + "|".join(PREFIJOS) + r")_[a-z0-9_]+)\b")
BASE = {
    "execute_code", "read_file", "write_file", "search_files", "patch", "skill_view", "skills_list", "memory", "todo",
    "terminal", "session_search", "clarify", "delegate_task", "send_message", "vision_analyze", "web_extract", "web_search",
}  # fmt: skip
SUGERENCIAS = (
    (("excel", "xlsx", "csv", "hoja de cálculo"), "hq_datos_extraer y hq_datos_consultar"),
    (("correo", "mail"), "ms365_mail_search / ms365_mail_list / ms365_mail_send"),
)


def _plano(texto):
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()


def _palabras(texto):
    return {p for p in re.split(r"[^a-z0-9]+", _plano(texto)) if len(p) > 3}


def herramientas_disponibles(raiz_codigo=None):
    nombres = set(BASE)
    try:
        from tools.registry import registry  # type: ignore

        nombres |= set(registry.get_all_tool_names())
    except Exception:  # noqa: BLE001  # fuera del runtime se usa el escaneo de archivos
        pass
    if raiz_codigo:
        for ruta in list(Path(raiz_codigo).glob("plugin_templates/*/__init__.py")) + list(
            Path(raiz_codigo).glob("integration_packages/*/plugin/__init__.py")
        ):
            try:
                texto = ruta.read_text(encoding="utf-8")
            except OSError:
                continue
            nombres |= set(re.findall(r'name="([a-z0-9_]+)"', texto)) | set(
                re.findall(r'"name": "([a-z0-9_]+)"', texto)
            )
    return nombres


def skills_administradas(raiz_skills):
    """{nombre: descripción} de las skills administradas (las que vienen con HermesHQ)."""
    salida = {}
    if not raiz_skills or not Path(raiz_skills).is_dir():
        return salida
    for carpeta in Path(raiz_skills).iterdir():
        archivo = carpeta / "SKILL.md"
        if carpeta.is_dir() and archivo.is_file():
            coincidencia = re.search(r'^description:\s*"?(.*?)"?\s*$', archivo.read_text(encoding="utf-8"), re.M)
            salida[carpeta.name] = coincidencia.group(1) if coincidencia else ""
    return salida


def _lista(valor):
    if isinstance(valor, str):
        return [valor] if valor.strip() else []
    return [v for v in (valor or []) if v is not None]


MAX_SCRIPTS, MAX_SCRIPT_CHARS = 3, 8000
SCRIPT_NOMBRE_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
IMPORTS_PROHIBIDOS = {
    "subprocess": "ejecutar programas del sistema", "socket": "abrir conexiones de red", "ftplib": "transferir archivos por red",
    "smtplib": "enviar correos por fuera de la plataforma (usa ms365_mail_send)", "telnetlib": "abrir conexiones de red",
    "ctypes": "llamar código nativo", "requests": "llamar a internet", "urllib": "llamar a internet", "httpx": "llamar a internet",
    "aiohttp": "llamar a internet", "http": "llamar a internet", "websocket": "abrir conexiones de red", "paramiko": "abrir conexiones remotas",
}  # fmt: skip
LLAMADAS_PROHIBIDAS = {"eval", "exec", "compile", "__import__"}


def normalizar_scripts(scripts):
    salida = []
    for item in _lista(scripts):
        if isinstance(item, dict):
            salida.append(
                {"nombre": str(item.get("nombre") or "").strip(), "contenido": str(item.get("contenido") or "")}
            )
    return salida


def validar_script(script):
    errores = []
    nombre, codigo = script["nombre"], script["contenido"]
    if not SCRIPT_NOMBRE_RE.match(nombre):
        errores.append(
            f"El script '{nombre}' necesita un nombre en minúsculas con guion bajo (p. ej. 'vigilar_correos')."
        )
    if not codigo.strip() or len(codigo) > MAX_SCRIPT_CHARS:
        errores.append(f"El script '{nombre}' debe tener entre 1 y {MAX_SCRIPT_CHARS} caracteres.")
        return errores
    try:
        arbol = ast.parse(codigo)
    except SyntaxError as exc:
        return [f"El script '{nombre}' tiene un error de sintaxis en la línea {exc.lineno}: {exc.msg}."]
    for nodo in ast.walk(arbol):
        modulos = []
        if isinstance(nodo, ast.Import):
            modulos = [a.name.split(".")[0] for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos = [nodo.module.split(".")[0]]
        for modulo in modulos:
            if modulo in IMPORTS_PROHIBIDOS:
                errores.append(f"El script '{nombre}' importa '{modulo}', no permitido: {IMPORTS_PROHIBIDOS[modulo]}.")
        if isinstance(nodo, ast.Call):
            funcion = nodo.func
            nombre_llamada = (
                funcion.id
                if isinstance(funcion, ast.Name)
                else (funcion.attr if isinstance(funcion, ast.Attribute) else "")
            )
            if nombre_llamada in LLAMADAS_PROHIBIDAS:
                errores.append(f"El script '{nombre}' usa {nombre_llamada}(), que no se permite.")
            if nombre_llamada in ("system", "popen") and isinstance(funcion, ast.Attribute):
                errores.append(f"El script '{nombre}' ejecuta comandos del sistema, no permitido.")
        if (
            isinstance(nodo, ast.Attribute)
            and nodo.attr in ("environ", "getenv")
            and isinstance(nodo.value, ast.Name)
            and nodo.value.id == "os"
        ):
            errores.append(f"El script '{nombre}' lee variables de entorno, no permitido (pueden contener claves).")
        if (
            isinstance(nodo, ast.Constant)
            and isinstance(nodo.value, str)
            and re.match(r"^(/|~|[A-Za-z]:\\)", nodo.value)
            and len(nodo.value) > 1
        ):
            errores.append(
                f"El script '{nombre}' usa la ruta '{nodo.value[:40]}': usa solo rutas dentro del espacio de trabajo (relativas)."
            )
    return list(dict.fromkeys(errores))


def normalizar_pasos(pasos):
    salida = []
    for paso in _lista(pasos):
        if isinstance(paso, dict):
            salida.append(
                {
                    "texto": str(paso.get("texto") or "").strip(),
                    "herramienta": str(paso.get("herramienta") or "").strip(),
                }
            )
        else:
            salida.append({"texto": str(paso).strip(), "herramienta": ""})
    return salida


def validar(skill, existentes_usuario, administradas, herramientas):
    errores, avisos = [], []
    nombre = str(skill.get("nombre") or "").strip()
    if not NOMBRE_RE.match(nombre):
        errores.append(
            "'nombre' debe ir en minúsculas con guiones, de 3 a 40 caracteres (p. ej. 'resumen-correos-semana')."
        )
    elif nombre in administradas:
        errores.append(f"'{nombre}' es una skill administrada; elige otro nombre o pide el cambio al administrador.")
    elif nombre in existentes_usuario and not skill.get("reemplazar"):
        errores.append(f"Ya existe tu skill '{nombre}'. Para modificarla pasa reemplazar=true (se guarda un respaldo).")
    descripcion = str(skill.get("descripcion") or "").strip()
    if not DESC_MIN <= len(descripcion) <= DESC_MAX:
        errores.append(
            f"'descripcion' debe tener entre {DESC_MIN} y {DESC_MAX} caracteres (tiene {len(descripcion)}): qué hace, en una frase."
        )
    frases = [str(f).strip() for f in _lista(skill.get("frases")) if str(f).strip()]
    if len(frases) < 2:
        errores.append("'frases' necesita al menos 2 formas en que el usuario lo pediría, con sus palabras.")
    pasos = normalizar_pasos(skill.get("pasos"))
    if not MAX_PASOS >= len(pasos) >= 2:
        errores.append(f"'pasos' debe tener entre 2 y {MAX_PASOS} pasos (tiene {len(pasos)}).")
    for n, paso in enumerate(pasos, 1):
        if not paso["texto"]:
            errores.append(f"El paso {n} está vacío.")
        if paso["herramienta"] and paso["herramienta"] not in herramientas:
            parecidas = difflib.get_close_matches(paso["herramienta"], sorted(herramientas), n=3, cutoff=0.6)
            sugerencia = f" ¿Quisiste decir {', '.join(parecidas)}?" if parecidas else ""
            errores.append(f"El paso {n} usa '{paso['herramienta']}', que no existe.{sugerencia}")
    scripts = normalizar_scripts(skill.get("scripts"))
    if len(scripts) > MAX_SCRIPTS:
        errores.append(f"Una skill admite hasta {MAX_SCRIPTS} scripts adjuntos.")
    for script in scripts[:MAX_SCRIPTS]:
        errores += validar_script(script)
    reglas = [str(r).strip() for r in _lista(skill.get("reglas")) if str(r).strip()]
    if len(reglas) > MAX_REGLAS:
        errores.append(f"'reglas' admite hasta {MAX_REGLAS}; resume.")
    if not str(skill.get("entrega") or "").strip():
        errores.append("'entrega' es obligatoria: qué se le entrega al usuario y cómo (archivo, resumen, correo…).")
    if not str(skill.get("ejemplo") or "").strip():
        errores.append(
            "'ejemplo' es obligatorio: una frase real con la que el usuario pediría esto (sirve para probarla)."
        )
    texto = " ".join(
        [descripcion, str(skill.get("entrega") or ""), str(skill.get("no_usar") or "")]
        + frases
        + reglas
        + [p["texto"] for p in pasos]
    )
    texto_minuscula = texto.lower()
    if "<html" in texto_minuscula or "<style" in texto_minuscula or "```" in texto:
        errores.append("La skill no lleva código, scripts ni HTML: solo pasos con herramientas existentes.")
    if re.search(r"(escrib\w+|crea\w*|genera\w*)\s+(un\s+)?(script|código|codigo|html|css|javascript)", _plano(texto)):
        errores.append(
            "No pidas escribir scripts, código ni HTML a mano: usa una herramienta existente o pide la capacidad al administrador."
        )
    if "skill_manage" in texto:
        errores.append("No menciones skill_manage: las skills se crean y editan solo con hq_skill_crear.")
    for token in sorted(set(TOKEN_HERRAMIENTA.findall(texto)) | {p["herramienta"] for p in pasos if p["herramienta"]}):
        if token not in herramientas:
            parecidas = difflib.get_close_matches(token, sorted(herramientas), n=3, cutoff=0.6)
            errores.append(
                f"Menciona la herramienta '{token}', que no existe."
                + (f" ¿Quisiste decir {', '.join(parecidas)}?" if parecidas else "")
            )
    errores = list(dict.fromkeys(errores))
    if len(componer(skill)) > MAX_CHARS:
        errores.append(f"La skill resultante pasa de {MAX_CHARS} caracteres; resume pasos y reglas.")
    mias = _palabras(descripcion + " " + " ".join(frases))
    for otra, desc in administradas.items():
        comunes = mias & _palabras(desc)
        if mias and len(comunes) / max(1, min(len(mias), len(_palabras(desc)))) >= 0.5 and len(comunes) >= 3:
            avisos.append(
                f"Se parece a la skill administrada '{otra}': revisa si ya cubre lo que quieres antes de crear otra."
            )
    for claves, herramienta in SUGERENCIAS:
        if any(c in texto_minuscula for c in claves) and not any(
            t in texto for t in re.findall(r"hq_[a-z_]+|ms365_[a-z_]+", herramienta)
        ):
            avisos.append(f"Para eso ya existe: {herramienta}. Úsala en el paso correspondiente.")
            break
    if re.search(r"\b[A-Za-z0-9+/=_-]{40,}\b", texto):
        avisos.append(
            "Hay un texto largo que parece un identificador copiado (ids, claves). No incluyas datos que cambian."
        )
    if not any(p["herramienta"] for p in pasos):
        avisos.append("Ningún paso indica una herramienta: sin herramientas la skill solo da instrucciones generales.")
    return errores, avisos


def _titulo(nombre):
    return nombre.replace("-", " ").capitalize()


def componer(skill):
    nombre = str(skill.get("nombre") or "").strip()
    descripcion = str(skill.get("descripcion") or "").strip().rstrip(".")
    frases = [str(f).strip() for f in _lista(skill.get("frases")) if str(f).strip()]
    pasos = normalizar_pasos(skill.get("pasos"))
    reglas = [str(r).strip() for r in _lista(skill.get("reglas")) if str(r).strip()]
    gatillos = ", ".join(f"'{f}'" for f in frases)
    lineas = [
        "---",
        f"name: {nombre}",
        f'description: "{descripcion}. Usar cuando digan {gatillos}."'.replace("\n", " "),
        "version: 1.0.0",
        "author: usuario (creada con hq_skill_crear)",
        "---",
        "",
        f"# {_titulo(nombre)}",
        "",
        "## Cuándo usarla",
    ]
    lineas += [f"- {f}" for f in frases]
    if str(skill.get("no_usar") or "").strip():
        lineas.append(f"- NO usar para: {str(skill['no_usar']).strip()}")
    lineas += ["", "## Flujo"]
    for n, paso in enumerate(pasos, 1):
        herramienta = f" (herramienta: `{paso['herramienta']}`)" if paso["herramienta"] else ""
        lineas.append(f"{n}. {paso['texto']}{herramienta}")
    lineas += [
        "",
        "## Reglas",
        "- Usa solo las herramientas indicadas; no escribas scripts ni HTML a mano.",
        "- No inventes cifras ni datos: todo sale de la fuente.",
    ]
    lineas += [f"- {r}" for r in reglas]
    scripts = normalizar_scripts(skill.get("scripts"))
    if scripts:
        lineas += ["", "## Scripts adjuntos (de esta skill; no los reescribas)"]
        for script in scripts:
            lineas.append(
                f"- `scripts/{script['nombre']}.py`: ejecútalo con execute_code: "
                f"`import runpy; runpy.run_path('{{RUTA}}/scripts/{script['nombre']}.py')`"
            )
    lineas += [
        "",
        "## Entrega",
        str(skill.get("entrega") or "").strip(),
        "",
        "## Ejemplo",
        f"> {str(skill.get('ejemplo') or '').strip()}",
        "",
    ]
    return "\n".join(lineas)


def carpeta_skills(hermes_home=None):
    base = Path(hermes_home) if hermes_home else Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return base / "skills"


def skills_de_usuario(raiz):
    salida = {}
    if raiz.is_dir():
        for archivo in raiz.glob("*/*/SKILL.md"):
            if archivo.parent.parent.name in ("hermeshq-managed",) or archivo.parent.parent.name.startswith("."):
                continue
            coincidencia = re.search(r'^description:\s*"?(.*?)"?\s*$', archivo.read_text(encoding="utf-8"), re.M)
            salida[archivo.parent.name] = {
                "categoria": archivo.parent.parent.name,
                "descripcion": coincidencia.group(1)[:160] if coincidencia else "",
            }
    return salida


def guardar(skill, raiz, espacio, categoria="usuario"):
    nombre = skill["nombre"]
    categoria = re.sub(r"[^a-z0-9-]", "", str(categoria or "usuario").lower()) or "usuario"
    existentes = skills_de_usuario(raiz)
    destino = raiz / existentes[nombre]["categoria"] / nombre if nombre in existentes else raiz / categoria / nombre
    respaldo = None
    if destino.exists():
        respaldos = raiz / ".respaldos"
        respaldos.mkdir(parents=True, exist_ok=True)
        marca = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        respaldo = respaldos / f"{nombre}-{marca}.md"
        shutil.copy2(destino / "SKILL.md", respaldo)
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "SKILL.md").write_text(componer(skill).replace("{RUTA}", str(destino)), encoding="utf-8")
    scripts = normalizar_scripts(skill.get("scripts"))
    if scripts:
        (destino / "scripts").mkdir(exist_ok=True)
        for script in scripts:
            (destino / "scripts" / f"{script['nombre']}.py").write_text(script["contenido"], encoding="utf-8")
    try:
        carpeta = Path(espacio) / "work"
        carpeta.mkdir(parents=True, exist_ok=True)
        with open(carpeta / ".skills_creadas.jsonl", "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "nombre": nombre,
                        "ruta": str(destino),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass
    return destino, respaldo


def retirar(nombre, raiz):
    existentes = skills_de_usuario(raiz)
    if nombre not in existentes:
        return None
    origen = raiz / existentes[nombre]["categoria"] / nombre
    retiradas = raiz / ".retiradas"
    retiradas.mkdir(parents=True, exist_ok=True)
    destino = retiradas / f"{nombre}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(origen), str(destino))
    return destino


def componer_ajuste(base, descripcion_base, reglas):
    reglas = [str(r).strip() for r in _lista(reglas) if str(r).strip()]
    return "\n".join(
        [
            "---",
            f"name: {base}-ajustes",
            f'description: "Ajustes personales del usuario sobre la skill administrada {base}. Léela junto con {base}."',
            "version: 1.0.0",
            "author: usuario (creada con hq_skill_crear)",
            "---",
            "",
            f"# Ajustes personales sobre {base}",
            f"Se suman a la skill administrada `{base}` ({str(descripcion_base)[:120]}). Si alguna regla contradice a la administrada, manda esta.",
            "",
            "## Reglas del usuario",
        ]
        + [f"- {r}" for r in reglas]
        + [""]
    )


def reglas_de_ajuste(base, raiz):
    archivo = Path(raiz) / "usuario" / f"{base}-ajustes" / "SKILL.md"
    if not archivo.is_file():
        return []
    texto = archivo.read_text(encoding="utf-8")
    cuerpo = texto.split("## Reglas del usuario", 1)[-1]
    return [linea[2:].strip() for linea in cuerpo.splitlines() if linea.startswith("- ")][: MAX_REGLAS * 2]


# ----------------------------------------------------------------------------------------------
# Enseñanza: consejos y guía
# ----------------------------------------------------------------------------------------------

GUIA = (
    "Cómo hacer una buena skill (para que cualquiera pueda usarla):\n"
    "1. Una skill = UN trabajo repetible con un resultado claro. Si hace tres cosas distintas, sepárala en tres.\n"
    "2. 'frases': escribe cómo lo pediría OTRA persona, no solo tú (3 o más variantes). Así el agente la reconoce.\n"
    "3. 'pasos': cada paso, una acción corta y la herramienta que usa. Si un paso no tiene herramienta, pregúntate si es una "
    "instrucción importante o relleno.\n"
    "4. Primero revisa qué ya existe (catálogo de capacidades): hay herramientas para Excel, correo, "
    "SharePoint y Teams. Usarlas es más rápido y estable que describir tú el trabajo.\n"
    "5. Nada personal dentro: ni tu correo, ni rutas con tu nombre, ni ids. Pide esos datos al usuario cada vez; así otra persona "
    "puede usarla.\n"
    "6. 'reglas': solo lo que NUNCA debe hacer o lo que siempre debe confirmar (enviar, borrar, crear).\n"
    "7. 'ejemplo': una frase real. Pruébala tú misma con 3 casos: uno normal, uno con datos faltantes y uno de otra sucursal o persona.\n"
    "8. Si necesitas lógica propia (contar, filtrar, vigilar), un script corto y validado va adjunto; si necesitas algo que hoy no "
    "existe en la plataforma, pídelo como capacidad.\n"
    "9. Para compartirla: sin datos personales, con un ejemplo claro. Las que llevan scripts las revisa el administrador."
)


def consejos(skill, errores, avisos):
    """Consejos concretos según lo que se ve en la skill; sirven para enseñar, no solo para corregir."""
    tips = []
    frases = [f for f in _lista(skill.get("frases")) if str(f).strip()]
    if len(frases) < 3:
        tips.append(
            "Agrega más variantes en 'frases' (cómo lo pediría otra persona): así el agente reconoce la skill con más facilidad."
        )
    pasos = normalizar_pasos(skill.get("pasos"))
    sin = [n for n, p in enumerate(pasos, 1) if not p["herramienta"]]
    if pasos and sin and len(sin) == len(pasos):
        tips.append(
            "Indica la herramienta de cada paso que use una: es lo que permite validarla y que otro la use igual."
        )
    elif sin:
        tips.append(
            f"Los pasos {', '.join(map(str, sin))} no nombran herramienta: confirma que son instrucciones y no trabajo que una herramienta ya hace."
        )
    if datos_personales(skill):
        tips.append(
            "Hay datos personales (correo, rutas con tu nombre): pídeselos al usuario en cada uso para poder compartirla."
        )
    if not errores:
        tips.append(
            "Pruébala con 3 casos antes de compartirla: uno normal, uno con datos faltantes y uno de otra sucursal o persona."
        )
    if not _lista(skill.get("reglas")):
        tips.append(
            "Piensa en una regla de seguridad: ¿qué debe confirmar siempre antes de enviar, crear o borrar algo?"
        )
    return tips[:5]


PERSONALES = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "un correo electrónico"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I), "una ruta de Windows con nombre de usuario"),
    (re.compile(r"OneDrive - [A-Za-z0-9 ]+", re.I), "una ruta de OneDrive personal"),
    (re.compile(r"\+?\d[\d\s-]{8,}\d"), "un número de teléfono o documento"),
)


def datos_personales(skill):
    texto = " ".join(
        [str(skill.get(k) or "") for k in ("descripcion", "entrega", "ejemplo", "no_usar")]
        + [str(f) for f in _lista(skill.get("frases"))]
        + [str(r) for r in _lista(skill.get("reglas"))]
        + [p["texto"] for p in normalizar_pasos(skill.get("pasos"))]
        + [str(x.get("contenido") or "") for x in normalizar_scripts(skill.get("scripts")) if isinstance(x, dict)]
    )
    return sorted({nombre for patron, nombre in PERSONALES if patron.search(texto)})


# ----------------------------------------------------------------------------------------------
# Biblioteca compartida
# ----------------------------------------------------------------------------------------------


def raiz_biblioteca():
    return Path(os.environ.get("HERMESHQ_BIBLIOTECA") or "/app/workspaces/_biblioteca")


def raiz_espacios():
    return Path(os.environ.get("HERMESHQ_WORKSPACES") or "/app/workspaces")


def _agentes_skills():
    return [p / ".hermes" / "skills" for p in sorted(raiz_espacios().glob("agent-*")) if (p / ".hermes").is_dir()]


def _copiar(origen, destino):
    if destino.exists():
        shutil.rmtree(destino)
    shutil.copytree(origen, destino, ignore=shutil.ignore_patterns(".hermeshq-skill.json", "__pycache__"))


def _difundir(nombre, carpeta_publicada):
    for raiz in _agentes_skills():
        _copiar(carpeta_publicada, raiz / "biblioteca" / nombre)


def _retirar_de_todos(nombre):
    for raiz in _agentes_skills():
        destino = raiz / "biblioteca" / nombre
        if destino.exists():
            shutil.rmtree(destino)


def _con_marca(texto, autor):
    marca = f"compartida_por: {autor}\ncompartida_el: {datetime.datetime.now():%Y-%m-%d}\n"
    return (
        texto.replace("\n---\n", "\n" + marca + "---\n", 1)
        if texto.startswith("---") and "compartida_por:" not in texto
        else texto
    )


def compartir(nombre, origen, autor):
    """Publica una skill de usuario. Sin scripts: queda disponible para todos al instante. Con scripts: queda pendiente de revisión."""
    biblioteca = raiz_biblioteca()
    publicadas = biblioteca / "skills"
    if (publicadas / nombre).exists() or (biblioteca / "pendientes" / nombre).exists():
        return {"estado": "existe"}
    tiene_scripts = any((origen / "scripts").glob("*.py")) if (origen / "scripts").is_dir() else False
    destino = (biblioteca / "pendientes" if tiene_scripts else publicadas) / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    _copiar(origen, destino)
    archivo = destino / "SKILL.md"
    archivo.write_text(_con_marca(archivo.read_text(encoding="utf-8"), autor), encoding="utf-8")
    if tiene_scripts:
        return {"estado": "pendiente", "ruta": str(destino)}
    _difundir(nombre, destino)
    return {"estado": "publicada", "ruta": str(destino)}


def aprobar(nombre):
    origen = raiz_biblioteca() / "pendientes" / nombre
    if not origen.is_dir():
        return False
    destino = raiz_biblioteca() / "skills" / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        shutil.rmtree(destino)
    shutil.move(str(origen), str(destino))
    _difundir(nombre, destino)
    return True


def rechazar(nombre, motivo=""):
    origen = raiz_biblioteca() / "pendientes" / nombre
    if not origen.is_dir():
        return False
    rechazadas = raiz_biblioteca() / "rechazadas"
    rechazadas.mkdir(parents=True, exist_ok=True)
    destino = rechazadas / f"{nombre}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(origen), str(destino))
    (destino / "MOTIVO.txt").write_text(motivo or "Sin motivo indicado", encoding="utf-8")
    return True


def autor_de(nombre):
    for carpeta in ("skills", "pendientes"):
        archivo = raiz_biblioteca() / carpeta / nombre / "SKILL.md"
        if archivo.is_file():
            coincidencia = re.search(r"^compartida_por:\s*(.+)$", archivo.read_text(encoding="utf-8"), re.M)
            return coincidencia.group(1).strip() if coincidencia else ""
    return None


def retirar_compartida(nombre):
    for carpeta in ("skills", "pendientes"):
        origen = raiz_biblioteca() / carpeta / nombre
        if origen.is_dir():
            retiradas = raiz_biblioteca() / "retiradas"
            retiradas.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origen), str(retiradas / f"{nombre}-{datetime.datetime.now():%Y%m%d-%H%M%S}"))
            _retirar_de_todos(nombre)
            return True
    return False


def listar_biblioteca():
    salida = {"publicadas": {}, "pendientes": []}
    publicadas = raiz_biblioteca() / "skills"
    if publicadas.is_dir():
        for archivo in publicadas.glob("*/SKILL.md"):
            texto = archivo.read_text(encoding="utf-8")
            descripcion = re.search(r'^description:\s*"?(.*?)"?\s*$', texto, re.M)
            autor = re.search(r"^compartida_por:\s*(.+)$", texto, re.M)
            salida["publicadas"][archivo.parent.name] = {
                "descripcion": descripcion.group(1)[:160] if descripcion else "",
                "autor": autor.group(1).strip() if autor else "",
            }
    pendientes = raiz_biblioteca() / "pendientes"
    if pendientes.is_dir():
        salida["pendientes"] = sorted(p.name for p in pendientes.iterdir() if p.is_dir())
    return salida


def datos_personales_en_carpeta(carpeta):
    encontrados = {}
    for archivo in [carpeta / "SKILL.md", *sorted((carpeta / "scripts").glob("*.py"))]:
        if not archivo.is_file():
            continue
        texto = archivo.read_text(encoding="utf-8", errors="ignore")
        for patron, nombre in PERSONALES:
            if patron.search(texto):
                encontrados.setdefault(nombre, []).append(archivo.name)
    return encontrados
