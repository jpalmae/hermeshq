import difflib
import re
import unicodedata

STRONG_WORDS = {
    "para",
    "pare",
    "parar",
    "parala",
    "paralo",
    "detente",
    "deten",
    "detener",
    "detenla",
    "detenlo",
    "cancela",
    "cancelar",
    "cancelala",
    "cancelalo",
    "cancelen",
    "cancelado",
    "aborta",
    "abortar",
    "abortala",
    "abortalo",
    "basta",
    "stop",
    "alto",
    "frena",
    "frenalo",
    "interrumpe",
    "interrumpir",
    "interrumpelo",
    "suspende",
    "suspender",
    "olvidalo",
    "olvidala",
}

STRONG_PHRASES = (
    "deja de",
    "no sigas",
    "no continues",
    "no continúes",
    "ya no sigas",
    "no sigas con",
    "ya no continues",
)

FILLER_WORDS = {
    "la",
    "lo",
    "el",
    "eso",
    "esto",
    "esa",
    "ese",
    "todo",
    "toda",
    "ya",
    "por",
    "favor",
    "porfa",
    "porfavor",
    "plis",
    "please",
    "gracias",
    "nomas",
    "no",
    "mas",
    "mejor",
    "ahora",
    "ahi",
    "tarea",
    "tareas",
    "proceso",
    "procesos",
    "trabajo",
    "que",
    "estas",
    "estoy",
    "estabas",
    "esta",
    "haciendo",
    "hacer",
    "hagas",
    "hace",
    "hacerlo",
    "de",
    "deja",
    "dejes",
    "sigas",
    "continues",
    "continua",
    "seguir",
    "continuar",
    "en",
    "curso",
    "actual",
    "actualmente",
    "anterior",
    "pedido",
    "solicitud",
    "peticion",
    "ejecucion",
    "ejecutando",
    "respuesta",
    "generacion",
    "y",
    "e",
    "un",
    "una",
    "ok",
    "oye",
    "hola",
    "ultrabot",
    "bot",
    "agente",
    "porque",
    "tardando",
    "tarda",
    "mucho",
    "demora",
    "demasiado",
    "lento",
    "sigue",
    "mismo",
    "ni",
    "nada",
    "me",
    "te",
    "se",
    "es",
    "momento",
    "pronto",
    "inmediato",
    "inmediatamente",
    "ahorita",
}

MAX_WORDS = 14


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9\s]", " ", stripped)


def _closest(token: str, vocabulary: set[str]) -> str | None:
    if token in vocabulary:
        return token
    if len(token) < 3:
        return None
    match = difflib.get_close_matches(token, vocabulary, n=1, cutoff=0.8)
    return match[0] if match else None


def is_cancel_request(text: str | None) -> bool:
    if not text:
        return False
    normalized = _normalize(text)
    tokens = normalized.split()
    if not tokens or len(tokens) > MAX_WORDS:
        return False
    phrase_hit = any(_normalize(phrase).strip() in normalized for phrase in STRONG_PHRASES)
    strong_hit = False
    for token in tokens:
        if token in STRONG_WORDS:
            strong_hit = True
            continue
        if token in FILLER_WORDS:
            continue
        if _closest(token, STRONG_WORDS):
            strong_hit = True
            continue
        if _closest(token, FILLER_WORDS):
            continue
        return False
    return strong_hit or phrase_hit


RESET_COMMANDS = {
    "/reset",
    "/nuevo",
    "/nueva",
    "/limpiar",
    "/new",
    "reset",
    "reiniciar conversacion",
    "reiniciar conversación",
}


def is_reset_request(text: str | None) -> bool:
    return bool(text) and text.strip().lower().rstrip(".!") in RESET_COMMANDS


RESET_PREFIX = re.compile(r"^\s*/(?:reset|nuevo|nueva|new|limpiar)\b[\s:,.-]*(.+)$", re.IGNORECASE | re.DOTALL)


def split_reset_prefix(text: str | None) -> str | None:
    match = RESET_PREFIX.match(text or "")
    return match.group(1).strip() if match and match.group(1).strip() else None


STATUS_PHRASES = (
    "como va",
    "como vas",
    "que paso",
    "que pasa",
    "que pasó",
    "por que demoras",
    "por que tardas",
    "por que te demoras",
    "porque demoras",
    "porque tardas",
    "estas ahi",
    "sigues ahi",
    "sigues trabajando",
    "sigue trabajando",
    "cuanto falta",
    "cuanto demora",
    "cuanto tardas",
    "ya esta",
    "ya terminaste",
    "novedades",
    "no obtuve respuesta",
    "no me responde",
    "no responde",
    "y el informe",
    "y mi informe",
    "y el entregable",
    "y la pptx",
    "y el archivo",
    "y el zip",
    "y la presentacion",
    "y el resultado",
    "estado de la solicitud",
    "estado de mi solicitud",
    "estado del informe",
)
STATUS_MAX_WORDS = 10


def is_status_request(text: str | None) -> bool:
    if not text:
        return False
    normalized = _normalize(text).strip()
    tokens = normalized.split()
    if not tokens or len(tokens) > STATUS_MAX_WORDS:
        return False
    padded = f" {' '.join(tokens)} "
    return any(f" {_normalize(phrase).strip()} " in padded for phrase in STATUS_PHRASES)
