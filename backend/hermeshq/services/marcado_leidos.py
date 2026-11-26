import re
import unicodedata

CLAVE_METADATA = "marcar_leidos_autorizado"

_DECLARACIONES = (
    re.compile(r"\bmarc\w*(?:\s+[^\s.;:!?\n]+){0,6}?\s+como\s+leid[oa]s?\b"),
    re.compile(r"\bmark\w*(?:\s+[^\s.;:!?\n]+){0,6}?\s+as\s+read\b"),
)
_NEGACION_PREVIA = re.compile(
    r"(?:^|[^\w])(?:no|ni|sin|nunca|jamas|evita\w*|prohib\w*|never|not|without|don'?t|do\s+not)"
    r"(?:\s+[^\s.,;:!?\n]+)?\s*$"
)
_NEGACION_INTERNA = re.compile(r"\b(?:no|ni|sin|nunca|jamas|never|not|without)\b")


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower()


def declara_marcar_leidos(texto: str | None) -> bool:
    normal = _normalizar(texto or "")
    for patron in _DECLARACIONES:
        for hallazgo in patron.finditer(normal):
            previo = normal[max(0, hallazgo.start() - 40) : hallazgo.start()]
            if not _NEGACION_PREVIA.search(previo) and not _NEGACION_INTERNA.search(hallazgo.group(0)):
                return True
    return False


def es_tarea_automatica(metadata: dict | None) -> bool:
    meta = metadata or {}
    return bool(meta.get("scheduled") or meta.get("background_job"))


def marcado_autorizado(metadata: dict | None, prompt: str | None) -> bool:
    meta = metadata or {}
    if not es_tarea_automatica(meta):
        return True
    if CLAVE_METADATA in meta:
        return meta.get(CLAVE_METADATA) is True
    return declara_marcar_leidos(prompt)


_SKILL_NOMBRADA = re.compile(r"\bskill\s+[«\"'`]?([a-z0-9][a-z0-9_.-]{2,80}[a-z0-9])", re.IGNORECASE)
_SKILLS_ADMINISTRADAS = "hermeshq-managed"
_MAX_SKILL_BYTES = 60_000


def skills_nombradas(prompt: str | None) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in _SKILL_NOMBRADA.finditer(prompt or "")))


def skill_propia_declara_marcar_leidos(prompt: str | None, workspace_path: str | None) -> bool:
    if not workspace_path:
        return False
    from pathlib import Path

    base = Path(workspace_path) / ".hermes" / "skills"
    for nombre in skills_nombradas(prompt):
        try:
            rutas = sorted(base.glob(f"*/{nombre}/SKILL.md"))
        except OSError:
            continue
        for ruta in rutas:
            if _SKILLS_ADMINISTRADAS in ruta.parts:
                continue
            try:
                texto = ruta.read_bytes()[:_MAX_SKILL_BYTES].decode("utf-8", errors="ignore")
            except OSError:
                continue
            if declara_marcar_leidos(texto):
                return True
    return False


def definicion_declara_marcar_leidos(schedule, workspace_path: str | None = None) -> bool:
    partes = (getattr(schedule, "prompt", None), getattr(schedule, "output_format", None))
    if any(isinstance(p, str) and declara_marcar_leidos(p) for p in partes):
        return True
    return skill_propia_declara_marcar_leidos(getattr(schedule, "prompt", None), workspace_path)
