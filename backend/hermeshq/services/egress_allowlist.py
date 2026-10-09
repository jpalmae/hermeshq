from __future__ import annotations

import logging
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import Settings, get_settings
from hermeshq.models.app_settings import AppSettings

logger = logging.getLogger(__name__)


def parse_env_allowlist(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    entries: list[str] = []
    for raw in (settings.runtime_egress_allowlist or "").split(","):
        entry = raw.strip().lower()
        if entry and entry not in entries:
            entries.append(entry)
    return entries


def domain_allowed(host: str, entries: list[str]) -> bool:
    host = host.lower().strip(".")
    for raw in entries:
        entry = raw.lower().strip()
        if not entry:
            continue
        if entry.startswith("."):
            entry = entry.lstrip(".")
            if host == entry or host.endswith(f".{entry}"):
                return True
        elif host == entry:
            return True
    return False


def extract_host(base_url: str) -> str | None:
    try:
        host = urlsplit(base_url.strip()).hostname
    except ValueError:
        return None
    return host.lower() if host else None


async def _load_extras(db: AsyncSession) -> list[str]:
    app_settings = await db.get(AppSettings, "default")
    if not app_settings:
        return []
    extras = getattr(app_settings, "egress_extra_allowlist", None) or []
    return [entry for entry in extras if isinstance(entry, str) and entry]


async def effective_allowlist(db: AsyncSession, settings: Settings | None = None) -> list[str]:
    combined = parse_env_allowlist(settings)
    for entry in await _load_extras(db):
        if entry not in combined:
            combined.append(entry)
    return combined


async def ensure_domain_allowed(
    db: AsyncSession,
    base_url: str,
    settings: Settings | None = None,
) -> tuple[bool, str | None]:
    host = extract_host(base_url) if base_url else None
    if not host:
        return False, None
    combined = await effective_allowlist(db, settings)
    if domain_allowed(host, combined):
        return False, host
    app_settings = await db.get(AppSettings, "default")
    if not app_settings:
        app_settings = AppSettings(id="default")
        db.add(app_settings)
    extras = getattr(app_settings, "egress_extra_allowlist", None) or []
    if host not in extras:
        extras = [*extras, host]
        app_settings.egress_extra_allowlist = extras
        await db.commit()
    logger.info("Auto-allowed provider endpoint host in runtime egress allowlist: %s", host)
    return True, host


async def push_allowlist_to_runner(client: object, db: AsyncSession, settings: Settings | None = None) -> int:
    domains = await effective_allowlist(db, settings)
    if not domains:
        return 0
    await client.push_egress_allowlist(domains)
    return len(domains)
