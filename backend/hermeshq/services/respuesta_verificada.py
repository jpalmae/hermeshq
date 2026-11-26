import json
import re
import unicodedata

ADJUNTO_SIN_ARCHIVO = re.compile(
    r"\b(te adjunto|te lo adjunto|te los adjunto|adjunto el|adjunto los|te envío adjunto|te envio adjunto)\b",
    re.IGNORECASE,
)
LINEA_MEDIA = re.compile(r"^\s*MEDIA:", re.MULTILINE)
AVISO_SIN_ADJUNTO = "⚠️ No pude adjuntar el archivo en este mensaje. Escribe «reintenta» para volver a intentarlo."
AFIRMA_ACCION = re.compile(
    r"\b(gener[ée]|verifiqu[ée]|prob[ée]|ejecut[ée]|comprob[ée]|"
    r"no tengo (acceso a |una |la |el )?(terminal|herramienta|permisos?))\b",
    re.IGNORECASE,
)
AVISO_SIN_HERRAMIENTA = (
    "⚠️ En esta respuesta no usé ninguna herramienta, así que no la tomes como verificada. "
    "Escribe «verifícalo» para que lo compruebe."
)
AFIRMA_ENVIO = re.compile(
    r"\b(correo (enviado|env[ií]ado)|enviado correctamente|enviado con [eé]xito|he enviado|ya (lo |la )?envi[eé]|"
    r"envi[eé] el correo|mand[eé] el correo|(fue|ha sido|qued[oó]) enviado|listo,? enviado|se envi[oó] (el correo|correctamente))\b",
    re.IGNORECASE,
)
AFIRMA_ENVIO_PRESENTE = re.compile(
    r"(?:^|[.!¡]\s+)\W*(env[íi]o|reenv[íi]o|mando)\s+(el|tu|ese|este|esa|esta)\s+(mensaje|correo|respuesta)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CONDICION = re.compile(r"\bcuando\b|\bapenas\b|\ben cuanto\b|\bsi (me )?(confirmas|dices|apruebas)", re.I)
_NEGADA = re.compile(r"\b(no|aún no|todavía no|nunca)\s+(lo |la |te |se )*$", re.IGNORECASE)
AVISO_ENVIO_NO_CONFIRMADO = (
    "⚠️ No hay confirmación de envío: ninguna herramienta de envío respondió con éxito en este mensaje. "
    "El correo o mensaje no se envió."
)
HERRAMIENTAS_ENVIO = ("ms365_mail_send", "ms365_mail_reply", "ms365_mail_forward")
CONFIRMAN_ENVIO = (*HERRAMIENTAS_ENVIO, "teams_send_chat_message", "teams_send_to_person")
MINIMO_PALABRAS = 4
UMBRAL_RESPALDO = 0.6


_ORACION = re.compile(r"(?<=[.!?\n])\s+")


def _promete_adjuntar(texto: str) -> bool:
    for oracion in _ORACION.split(texto):
        if "?" in oracion or "¿" in oracion:
            continue
        if ADJUNTO_SIN_ARCHIVO.search(oracion):
            return True
    return False


def con_aviso_de_adjunto(texto: str | None) -> str | None:
    if not texto or not _promete_adjuntar(texto) or LINEA_MEDIA.search(texto):
        return texto
    return f"{texto.rstrip()}\n\n{AVISO_SIN_ADJUNTO}"


_AVISOS_FINALES = (
    AVISO_SIN_ADJUNTO,
    "⚠️ En esta respuesta no usé ninguna herramienta, así que no la tomes como verificada. "
    "Escribe «verifícalo» para que lo compruebe.",
)
_AVISO_INICIAL = re.compile(r"\A⚠️ Revisé lo que se ejecutó y corregí esta respuesta:[^\n]*(?:\n\s*\n)?")


def sin_avisos_de_la_plataforma(texto: str | None) -> str | None:
    if not texto:
        return texto
    limpio = _AVISO_INICIAL.sub("", texto)
    for aviso in _AVISOS_FINALES:
        if aviso in limpio:
            limpio = limpio.replace(aviso, "")
    return limpio.strip() or texto


ENLACE = re.compile(r"https?://[^\s)\]>»\"'`]+", re.IGNORECASE)
AFIRMA_ENLACE_PROPIO = re.compile(
    r"\b(gener[ée]|cre[ée]|prepar[ée]|compart[ií]|sub[ií])\b[^.\n]{0,60}\b(enlace|link|v[ií]nculo)\b|"
    r"\b(enlace|link)\b[^.\n]{0,40}\b(generado|creado|listo)\b",
    re.IGNORECASE,
)
AVISO_ENLACE_INVENTADO = (
    "⚠️ Quité un enlace que escribí sin usar ninguna herramienta: no lo generé y no existe. "
    "Escribe «compártelo» o «genera el enlace» para que lo cree de verdad."
)


def con_aviso_sin_herramienta(texto: str | None, herramientas_usadas: int, contexto: str | None = None) -> str | None:
    if not texto or herramientas_usadas:
        return texto
    conocidos = set(ENLACE.findall(contexto or ""))
    inventados = [u for u in ENLACE.findall(texto) if u not in conocidos] if AFIRMA_ENLACE_PROPIO.search(texto) else []
    if inventados:
        for url in dict.fromkeys(inventados):
            texto = texto.replace(url, "[enlace no verificado]")
        return f"{texto.rstrip()}\n\n{AVISO_ENLACE_INVENTADO}"
    if not AFIRMA_ACCION.search(texto):
        return texto
    return f"{texto.rstrip()}\n\n{AVISO_SIN_HERRAMIENTA}"


def _palabras(texto: str) -> list[str]:
    limpio = unicodedata.normalize("NFKD", texto.lower())
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]{3,}", limpio)


def parrafos_sin_respaldo(generados: list[str], fuentes: list[str]) -> list[str]:
    corpus = {palabra for fuente in fuentes for palabra in _palabras(fuente)}
    sin_respaldo = []
    for parrafo in generados:
        palabras = _palabras(parrafo)
        if len(palabras) < MINIMO_PALABRAS:
            continue
        presentes = sum(1 for palabra in palabras if palabra in corpus) / len(palabras)
        if presentes < UMBRAL_RESPALDO:
            sin_respaldo.append(parrafo.strip())
    return sin_respaldo


def envios_confirmados(messages: list | None) -> int:
    confirmados = 0
    for mensaje in messages or []:
        if mensaje.get("role") != "tool" or mensaje.get("name") not in CONFIRMAN_ENVIO:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(resultado, dict)
            and resultado.get("success") is True
            and not resultado.get("requiere_aprobacion")
        ):
            confirmados += 1
    return confirmados


ESCRITURAS_GRAPH = (
    "ms365_calendar_create_event",
    "ms365_calendar_update_event",
    "ms365_calendar_delete_event",
    "ms365_calendar_respond_event",
    "sharepoint_share_file",
    "sharepoint_upload_file",
)
AFIRMA_ACCION_GRAPH = re.compile(
    r"\b(reuni[oó]n (creada|agendada|programada|cre[eé]|a[nñ]adida)|evento (creado|eliminado|actualizado|cancelado|movido)|"
    r"(he |la |lo )?(creado|agendado) (la |el )?(reuni[oó]n|evento)|enlace (creado|compartido|generado)|"
    r"(lo |la |te lo )?he compartido|(fue|ha sido|qued[oó]) compartid[oa]|(listo|hecho),? compartid[oa]|"
    r"invitaci[oó]n (aceptada|rechazada|respondida)|mensaje (enviado|publicado) (en|a) teams)\b",
    re.IGNORECASE,
)
AVISO_ACCION_GRAPH_NO_CONFIRMADA = (
    "⚠️ No hay confirmación en Microsoft 365: ninguna herramienta de calendario o de compartir respondió con éxito "
    "en este mensaje. Si la acción no se ha hecho, dime y la vuelvo a pedir para tu aprobación."
)


def escrituras_graph_confirmadas(messages: list | None) -> int:
    confirmadas = 0
    for mensaje in messages or []:
        if mensaje.get("role") != "tool" or mensaje.get("name") not in ESCRITURAS_GRAPH:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(resultado, dict)
            and resultado.get("success") is True
            and not resultado.get("requiere_aprobacion")
            and resultado.get("estado") != "desconocido"
        ):
            confirmadas += 1
    return confirmadas


_CAMPO_DE_VISTA = re.compile(
    r"^\s*[-*]\s*\*\*(descripci[oó]n|t[ií]tulo|reuni[oó]n|nuevo t[ií]tulo)\s*:\*\*.*$", re.I | re.M
)


def _vista_pendiente(messages: list | None) -> bool:
    for mensaje in messages or []:
        if mensaje.get("role") != "tool" or mensaje.get("name") not in ESCRITURAS_GRAPH:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(resultado, dict) and resultado.get("requiere_aprobacion"):
            return True
    return False


def con_aviso_accion_graph_no_confirmada(
    texto: str | None, messages: list | None, aprobacion_ejecutada_en_servidor: bool = False
) -> str | None:
    if not texto:
        return texto
    afirmaciones = _CAMPO_DE_VISTA.sub("", texto)
    if (
        not AFIRMA_ACCION_GRAPH.search(afirmaciones)
        or aprobacion_ejecutada_en_servidor
        or escrituras_graph_confirmadas(messages)
        or (_vista_pendiente(messages) and "vista previa" in texto.lower())
    ):
        return texto
    return f"{texto.rstrip()}\n\n{AVISO_ACCION_GRAPH_NO_CONFIRMADA}"


AFIRMA_DESCARTE = re.compile(
    r"\b(descart[eéó]|descartad[oa]s?|resolv[ií]|resuelt[oa]s?|marqu[eé] como resuelt[oa]s?|sali[oó] de la lista|"
    r"salieron de la lista|ya no aparece)\b",
    re.IGNORECASE,
)
AFIRMA_MARCADO_LEIDO = re.compile(
    r"\b(marqu[eé]|marcad[oa]s?|marc[oó])\b[^.\n]{0,30}\bcomo\s+le[ií]d[oa]s?\b|\bleíd[oa]s?\b[^.\n]{0,12}\bmarcad",
    re.IGNORECASE,
)
_NEGADO_CERCA = re.compile(r"\b(no|sin|nunca|todavía no|aún no|ni)\b[^.\n]{0,25}$", re.IGNORECASE)
AVISO_DESCARTE_NO_CONFIRMADO = (
    "⚠️ Revisé lo que se ejecutó: ningún correo se movió a descartados o resueltos en este mensaje (la herramienta "
    "informó 0 movidos), así que lo anterior no ocurrió. Los #ID pueden estar mal o ya haber estado en ese estado; "
    "dime cuáles y lo reviso."
)
AVISO_MARCADO_NO_CONFIRMADO = (
    "⚠️ Revisé lo que se ejecutó: no se marcó ningún correo como leído en este mensaje (no se llamó la herramienta que "
    "lo hace). Siguen sin leer en Outlook; si quieres que los marque, dímelo."
)


def _afirma_sin_negar(patron, texto: str) -> bool:
    for c in patron.finditer(texto or ""):
        if _NEGADO_CERCA.search(texto[max(0, c.start() - 30) : c.start()]):
            continue
        return True
    return False


def _resultados_de(messages: list | None, herramienta: str) -> list[dict]:
    resultados = []
    for mensaje in messages or []:
        if not isinstance(mensaje, dict) or mensaje.get("role") != "tool" or mensaje.get("name") != herramienta:
            continue
        try:
            valor = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(valor, dict):
            resultados.append(valor)
    return resultados


def _sync_marco_leidos(resultados: list[dict]) -> bool:
    for r in resultados:
        bloque = r.get("marcados_leidos")
        cuenta = bloque.get("marcados") if isinstance(bloque, dict) else r.get("marcados")
        try:
            if int(cuenta or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def con_aviso_descarte_o_marcado_no_confirmado(texto: str | None, messages: list | None) -> str | None:
    if not texto:
        return texto
    avisos = []
    sync = _resultados_de(messages, "ms365_correo_sync")
    moviendo = [r for r in sync if "movidos" in r]
    if _afirma_sin_negar(AFIRMA_DESCARTE, texto):
        if (
            moviendo
            and not any(int(r.get("movidos") or 0) > 0 for r in moviendo)
            and not any(r.get("ya_estaban_en_ese_estado") for r in moviendo)
        ):
            avisos.append(AVISO_DESCARTE_NO_CONFIRMADO)
    if _afirma_sin_negar(AFIRMA_MARCADO_LEIDO, texto):
        marcados = [r for r in _resultados_de(messages, "ms365_mail_mark_read") if r.get("success") is True]
        if not marcados and not _sync_marco_leidos(sync):
            avisos.append(AVISO_MARCADO_NO_CONFIRMADO)
    if not avisos:
        return texto
    return f"{texto.rstrip()}\n\n" + "\n".join(avisos)


PIDE_ENVIAR = re.compile(r"\b(s[ií]|ok|dale|env[ií]a\w*|enviar\w*|manda\w*|confirmo|adelante)\b", re.IGNORECASE)
RESPUESTA_NO_ENVIADO = (
    "⚠️ El correo NO se envió: el envío no llegó a ejecutarse. Responde «envía» y lo envío ahora "
    "(o dime qué quieres cambiar)."
)


def _pide_enviar(prompt: str | None) -> bool:
    texto = re.sub(r"(?s)^\[Respuesta corta a tu último mensaje:.*?no a temas anteriores\.\]\s*", "", prompt or "")
    texto = texto.strip()
    return bool(texto) and len(texto) <= 120 and "?" not in texto and bool(PIDE_ENVIAR.search(texto))


def afirma_envio(texto: str) -> bool:
    for patron in (AFIRMA_ENVIO, AFIRMA_ENVIO_PRESENTE):
        for c in patron.finditer(texto or ""):
            if _NEGADA.search(texto[max(0, c.start() - 25) : c.start()]):
                continue
            resto = re.split(r"[.!?\n]", texto[c.end() :], maxsplit=1)[0]
            if patron is AFIRMA_ENVIO_PRESENTE and _CONDICION.search(resto):
                continue
            return True
    return False


def con_aviso_envio_no_confirmado(texto: str | None, messages: list | None, prompt: str | None = None) -> str | None:
    if not texto or not afirma_envio(texto) or envios_confirmados(messages):
        return texto
    if _pide_enviar(prompt):
        return RESPUESTA_NO_ENVIADO
    return f"{texto.rstrip()}\n\n{AVISO_ENVIO_NO_CONFIRMADO}"


def borrador_pendiente(messages: list | None) -> dict | None:
    pendiente = None
    for mensaje in messages or []:
        if mensaje.get("role") != "tool" or mensaje.get("name") not in HERRAMIENTAS_ENVIO:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(resultado, dict):
            continue
        if resultado.get("requiere_aprobacion") and resultado.get("borrador"):
            pendiente = resultado
        elif resultado.get("success") is True:
            pendiente = None
    return pendiente


def con_borrador_canonico(texto: str | None, messages: list | None) -> str | None:
    pendiente = borrador_pendiente(messages)
    if not pendiente:
        return texto
    partes = [str(pendiente["borrador"]).strip()]
    verificacion = (pendiente.get("vista_previa") or {}).get("verificacion_directorio") or {}
    avisos = [f"{correo}: {etiqueta}" for correo, etiqueta in verificacion.items() if str(etiqueta).startswith("⚠")]
    if avisos and not pendiente.get("advertencia_destinatarios"):
        partes.append("⚠️ Revisa estos destinatarios: " + "; ".join(avisos))
    partes.append("¿Lo envío?")
    return "\n\n".join(partes)


_CATEGORIAS_INFORME = ("INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO", "FRÍO")
_SIN_CORREOS = "_(sin correos nuevos en esta ventana)_"


def tablas_de_correo(messages: list | None) -> dict | None:
    tablas = None
    for mensaje in messages or []:
        if mensaje.get("role") != "tool" or mensaje.get("name") != "ms365_correo_sync":
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(resultado, dict) and isinstance(resultado.get("tablas_md"), dict):
            tablas = resultado["tablas_md"]
    return tablas


def _categoria_del_titulo(linea: str) -> str | None:
    limpia = linea.strip()
    if not limpia or len(limpia) > 60 or limpia.startswith("|") or "CONSOLIDADO" in limpia.upper():
        return None
    sin_tildes = unicodedata.normalize("NFKD", limpia).encode("ascii", "ignore").decode("ascii").upper()
    for categoria in _CATEGORIAS_INFORME:
        clave = unicodedata.normalize("NFKD", categoria).encode("ascii", "ignore").decode("ascii")
        if re.match(r"^[^A-Z0-9]*" + clave + r"\b", sin_tildes):
            return categoria
    return None


_LECTURAS_DE_BUZON = {"ms365_correo_sync": ("ventana_leida", "tablas_md"), "ms365_mail_list": ("messages",)}
_FILA_DE_CONTEO = re.compile(
    r"^\s*\|[^|]*\b(INMEDIATO|CR[IÍ]TICO|CALIENTE|TIBIO|FR[IÍ]OS?|OTROS)\b[^|]*\|\s*\d+\s*\|\s*$", re.IGNORECASE
)
_ENCABEZADO_DE_CONTEO = re.compile(r"^\s*\|\s*Categor[ií]a\s*\|\s*(Cantidad|Total|N\.?º?)\s*\|\s*$", re.IGNORECASE)
_SEPARADOR_DE_TABLA = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_SIN_CORREOS_INVENTADO = re.compile(
    r"sin correos nuevos en esta ventana|^\W*\d+\s+correos?\s+fr[ií]os?\s+consolidados", re.IGNORECASE
)
AVISO_BUZON_NO_LEIDO = "_No hay conteos: no se pudo leer el buzón (no es que no haya correos)._"


def lectura_de_buzon_fallida(messages: list | None) -> bool:
    fallida = False
    for mensaje in messages or []:
        claves = _LECTURAS_DE_BUZON.get(str(mensaje.get("name") or "")) if mensaje.get("role") == "tool" else None
        if not claves:
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(resultado, dict):
            continue
        if resultado.get("success") is True and any(k in resultado for k in claves):
            return False
        if resultado.get("success") is False:
            fallida = True
    return fallida


def con_informe_sin_lectura(texto: str | None, messages: list | None) -> str | None:
    if not texto or not lectura_de_buzon_fallida(messages):
        return texto
    lineas = texto.split("\n")
    quitar = {
        i
        for i, linea in enumerate(lineas)
        if _FILA_DE_CONTEO.match(linea) or _ENCABEZADO_DE_CONTEO.match(linea) or _SIN_CORREOS_INVENTADO.search(linea)
    }
    if not quitar:
        return texto
    for i, linea in enumerate(lineas):
        if _SEPARADOR_DE_TABLA.match(linea) and ((i - 1) in quitar or (i + 1) in quitar):
            quitar.add(i)
    for i, linea in enumerate(lineas):
        if i in quitar or not (_categoria_del_titulo(linea) or "CONSOLIDADOS" in linea.upper()):
            continue
        siguiente = next((j for j in range(i + 1, len(lineas)) if lineas[j].strip()), None)
        if siguiente is None or siguiente in quitar:
            quitar.add(i)
    salida = [linea for i, linea in enumerate(lineas) if i not in quitar]
    resultado = re.sub(r"\n{3,}", "\n\n", "\n".join(salida)).strip()
    return f"{resultado}\n\n{AVISO_BUZON_NO_LEIDO}"


def con_tablas_de_correo(texto: str | None, messages: list | None) -> str | None:
    tablas = tablas_de_correo(messages)
    if not texto or not tablas:
        return texto
    lineas = texto.split("\n")
    salida: list[str] = []
    i = 0
    while i < len(lineas):
        linea = lineas[i]
        salida.append(linea)
        i += 1
        categoria = _categoria_del_titulo(linea)
        if not categoria or categoria not in tablas:
            continue
        while i < len(lineas) and not lineas[i].strip():
            i += 1
        while i < len(lineas):
            actual = lineas[i].strip()
            if not actual:
                siguiente = next((x.strip() for x in lineas[i:] if x.strip()), "")
                if siguiente.startswith("|") or siguiente.startswith("_(sin correos"):
                    i += 1
                    continue
                break
            if actual.startswith("|") or actual.startswith("_(sin correos") or actual.startswith(("- ", "• ", "* ")):
                i += 1
                continue
            break
        salida.append(str(tablas[categoria]))
        salida.append("")
    return "\n".join(salida).rstrip() + ("\n" if texto.endswith("\n") else "")
