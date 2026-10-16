import re
import unicodedata

MAX_TITLE_CHARS = 160
MAX_TEXT_CHARS = 3000
MAX_FILES = 8
MAX_REQUESTS_PER_AGENT_PER_DAY = 8


def fingerprint(title: str) -> str:
    plano = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    palabras = [p for p in re.split(r"[^a-z0-9]+", plano) if len(p) > 2]
    return "-".join(sorted(set(palabras))[:8])[:80] or "sin-titulo"


def clean_text(valor, limite: int = MAX_TEXT_CHARS) -> str:
    return str(valor or "").strip()[:limite]


def clean_files(valor) -> list[str]:
    if not isinstance(valor, list):
        return []
    return [str(x).strip()[:200] for x in valor if str(x).strip()][:MAX_FILES]
