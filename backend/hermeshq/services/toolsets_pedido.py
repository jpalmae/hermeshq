import re
import unicodedata

NUCLEO = [
    "safe",
    "file",
    "skills",
    "memory",
    "todo",
    "code_execution",
    "vision",
    "delegation",
    "messaging",
    "hermeshq_comms",
    "hermeshq_schedule",
]

PRODUCTO = {
    "hermeshq_xlsx": r"excel|xlsx|xlsm|csv|hoja|planilla|reproceso",
    "hermeshq_documentos": r"excel|xlsx|csv|datos|informe|dashboard|reporte|documento|presentacion|diapositiva|lamina|slide|pdf|word|docx|pptx|powerpoint|skill|habilidad|procedimiento|politica|sgsi|manual"
    r"|imagen|foto|ilustra|logo|dibuj",
    "hermeshq_pptx": r"presentacion|diapositiva|slide|pptx|powerpoint|comite|lamina|deck",
    "hermeshq_docx": r"word|docx|documento|procedimiento|manual|politica|sgsi",
    "hermeshq_pdf": r"\bpdf\b",
    "hermeshq_audio": r"audio|voz|transcrib",
    "image_gen": r"imagen|foto|ilustra|logo",
    "hermeshq_ms365_mail": r"correo|mail|email|bandeja|reenviar|reunion|[\w.+-]+@[\w-]+\.[\w.-]+|(?s:borrador.*envi|envi.*borrador)",
    "hermeshq_ms365_calendar": r"calendario|agenda|reunion|\breu\b|asistencia|invitacion|\bcitas?\b",
    "hermeshq_ms365_teams": r"teams",
    "hermeshq_sharepoint": r"sharepoint|onedrive|compart|enlace|\blink\b|vinculo|\bsub(e|ir|elo|ela|irlo|irla)\b",
    "browser": r"navega|sitio web|url|http|pagina web",
    "hermeshq_lote": r"lote|masivo|todos los archivos",
}

ADJUNTO = "archivos adjuntos"
INTEGRACIONES_DE_DATOS = {
    "hermeshq_sharepoint",
    "hermeshq_ms365_mail",
    "hermeshq_ms365_calendar",
    "hermeshq_ms365_teams",
}
MARCAS_DE_SEGUIMIENTO = ("respuesta corta a tu ultimo mensaje",)
RESPUESTA_A_PREGUNTA = re.compile(r"\s*(si|ok|dale|listo|claro|perfecto|vale)\b")
UBICACION = r"sharepoint|onedrive|carpeta|ubicacion"
ARCHIVO_NOMBRADO = r"archivo|\.xlsx|\.xlsm|\.csv|\.docx|\.pptx|\.pdf"
BUSCAR_ALGO = (
    r"\b(busca|buscame|buscalo|buscala|buscalos|buscalas|encuentra|encuentrame|encuentralo|encuentrala|localiza|"
    r"localizame|ubica|ubicame|rastrea)\b"
    r"|\bbuscar (el|la|los|las|un|una|todo|algo|archivos?|documentos?)\b"
    r"|\bdonde (esta|estan|quedo|quedaron|dejaste|guardaste)\b"
)
OTRAS_INTEGRACIONES = {"hermeshq_ms365_mail", "hermeshq_ms365_calendar", "hermeshq_ms365_teams"}
NECESITA_FUENTE = (
    r"(?s)(?=.*\b(genera\w*|crea\w*|haz|hazme|hacer|arma\w*|prepara\w*|construye\w*|elabora\w*|necesito|quiero|dame)\b)"
    r"(?=.*(informe|reporte|dashboard|tablero|indicador|\bkpi|analisis|estadistic|accidentab|\bhse\b))"
)
ORIGENES = ("public_chat", "background_job")
HERRAMIENTAS_DE_INTEGRACION = (
    (r"^ms365_(mail_|correo_|inbox_|find_person)", "hermeshq_ms365_mail"),
    (r"^ms365_calendar_", "hermeshq_ms365_calendar"),
    (r"^teams_", "hermeshq_ms365_teams"),
    (r"^sharepoint_", "hermeshq_sharepoint"),
)


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return sin_tildes.lower()


def _productos_del_pedido(pedido: str) -> set[str]:
    elegidos = {nombre for nombre, patron in PRODUCTO.items() if re.search(patron, pedido)}
    busca_algo = bool(re.search(BUSCAR_ALGO, pedido)) and not (elegidos & OTRAS_INTEGRACIONES)
    if re.search(UBICACION, pedido) or (
        (re.search(ARCHIVO_NOMBRADO, pedido) or re.search(NECESITA_FUENTE, pedido) or busca_algo)
        and ADJUNTO not in pedido
    ):
        elegidos.add("hermeshq_sharepoint")
    return elegidos


def integraciones_usadas(historial) -> set[str]:
    nombres = [str(n) for n in (getattr(historial, "herramientas", None) or [])]
    return {toolset for patron, toolset in HERRAMIENTAS_DE_INTEGRACION if any(re.match(patron, n) for n in nombres)}


def toolsets_para_pedido(
    texto: str, enabled_toolsets: list[str] | None, usadas: set[str] | frozenset = frozenset()
) -> list[str] | None:
    if not enabled_toolsets:
        return enabled_toolsets
    pedido = _normalizar(texto)
    if any(marca in pedido for marca in MARCAS_DE_SEGUIMIENTO) or RESPUESTA_A_PREGUNTA.match(pedido):
        return enabled_toolsets
    elegidos = _productos_del_pedido(pedido)
    if not elegidos:
        return [t for t in enabled_toolsets if t not in INTEGRACIONES_DE_DATOS or t in usadas]
    permitidos = set(NUCLEO) | elegidos | set(usadas)
    return [t for t in enabled_toolsets if t in permitidos or t not in PRODUCTO]


MENSAJES_DE_CONTEXTO = 2


def _pedidos_recientes(historial) -> str:
    usuario = [
        str(item.get("content") or "")
        for item in (historial or [])
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    return " ".join(usuario[-MENSAJES_DE_CONTEXTO:])


def filtrar_para_tarea(task, enabled_toolsets: list[str] | None, historial=None) -> list[str] | None:
    metadata = getattr(task, "metadata_json", None) or {}
    if metadata.get("source") not in ORIGENES:
        return enabled_toolsets
    pedido = getattr(task, "prompt", "") or ""
    contexto = _pedidos_recientes(historial)
    if contexto:
        pedido = f"{pedido}\n{contexto}"
    return toolsets_para_pedido(pedido, enabled_toolsets, integraciones_usadas(historial))


SENALES_NUCLEO = {
    "code_execution": r"codigo|script|python|programa en|calcul|formula|grafico|graficar|estadistic",
    "vision": r"imagen|foto|captura|pantallazo|\.png|\.jpe?g|\.webp|diagrama",
    "delegation": r"delega|subagente",
    "hermeshq_comms": r"otro agente|agentes|delega|avisale a",
    "hermeshq_schedule": r"programa(r|lo|la)? |cada (dia|lunes|martes|miercoles|jueves|viernes|semana|mes|hora)|recordatorio|todos los (dias|lunes)|cron|tarea programada",
    "browser": r"navega|sitio web|url|http|pagina web",
}


EDICION = (
    r"modific|edit|cambi|corrig|arregl|actualiz|reemplaz|ajust|agreg|quit|elimin|borr|en blanco|vaci|no se ve|falta"
)
EDITAR_WORD = rf"(?s)(?=.*(?:{EDICION}))(?=.*(?:word|docx|documento))"

GRUPOS_DE_HERRAMIENTAS = {
    "hermeshq_documentos": {
        r"\bpdf\b|word|docx|pptx|powerpoint|excel|xlsx|planilla|leer|lee |resume": ["hq_leer_documento"],
        EDITAR_WORD: ["hq_docx_editar"],
    },
}


def herramientas_de_grupos(texto_normalizado: str, pedidos: list[str]) -> tuple[list[str], list[str]]:
    completos, sueltas = [], []
    for toolset in pedidos:
        grupos = GRUPOS_DE_HERRAMIENTAS.get(toolset)
        elegidas = [h for patron, hs in (grupos or {}).items() if re.search(patron, texto_normalizado) for h in hs]
        if elegidas:
            sueltas.extend(h for h in elegidas if h not in sueltas)
        else:
            completos.append(toolset)
    return completos, sueltas


def seleccion_progresiva(
    task, enabled_toolsets: list[str] | None, historial=None
) -> tuple[list[str] | None, list[str]]:
    habilitados, pedidos, _sueltas = seleccion_progresiva_detallada(task, enabled_toolsets, historial)
    return habilitados, pedidos


def seleccion_progresiva_detallada(task, enabled_toolsets: list[str] | None, historial=None):
    if not enabled_toolsets:
        return enabled_toolsets, [], []
    metadata = getattr(task, "metadata_json", None) or {}
    pedido = getattr(task, "prompt", "") or ""
    contexto = _pedidos_recientes(historial)
    texto = _normalizar(f"{pedido}\n{contexto}" if contexto else pedido)
    elegidos = _productos_del_pedido(texto) | integraciones_usadas(historial)
    elegidos |= {t for t, patron in SENALES_NUCLEO.items() if re.search(patron, texto)}
    if metadata.get("background_job") or metadata.get("source") == "background_job":
        elegidos |= {"code_execution", "file"}
    if metadata.get("source") in ORIGENES:
        habilitados = [t for t in enabled_toolsets if t not in INTEGRACIONES_DE_DATOS or t in elegidos]
    else:
        habilitados = list(enabled_toolsets)
    pedidos = [t for t in habilitados if t in elegidos]
    completos, sueltas = herramientas_de_grupos(texto, pedidos)
    return habilitados, completos, sueltas


def exposicion_activa(agent, ajustes=None) -> bool:
    if ajustes is None:
        from hermeshq.config import get_settings

        ajustes = get_settings()
    valor = str(getattr(ajustes, "tool_exposure_progressive_agents", "") or "").strip()
    if not valor:
        return False
    if valor == "*":
        return True
    nombres = {v.strip() for v in valor.split(",") if v.strip()}
    return bool(
        {str(getattr(agent, "id", "")), str(getattr(agent, "name", "")), str(getattr(agent, "slug", ""))} & nombres
    )
