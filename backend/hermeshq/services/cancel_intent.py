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
