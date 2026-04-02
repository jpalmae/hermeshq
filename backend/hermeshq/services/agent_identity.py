import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.agent import Agent


def slugify_agent_value(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").strip())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug[:96] or "agent"


def derive_agent_identity(
    *,
    friendly_name: str | None,
    name: str | None,
    slug: str | None,
) -> tuple[str, str, str]:
    resolved_friendly = (friendly_name or "").strip()
    resolved_name = (name or "").strip() or resolved_friendly or "Agent"
    resolved_friendly = resolved_friendly or resolved_name
    resolved_slug = slugify_agent_value((slug or "").strip() or resolved_friendly or resolved_name)
    return resolved_friendly, resolved_name, resolved_slug


async def ensure_unique_agent_slug(
    db: AsyncSession,
    slug: str,
    *,
    exclude_agent_id: str | None = None,
) -> str:
    base_slug = slugify_agent_value(slug)
    candidate = base_slug
    suffix = 2
    while True:
        query = select(Agent.id).where(Agent.slug == candidate)
        if exclude_agent_id:
            query = query.where(Agent.id != exclude_agent_id)
        existing = (await db.execute(query)).scalar_one_or_none()
        if not existing:
            return candidate
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
