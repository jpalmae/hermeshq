from __future__ import annotations

import logging

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.ssh_destination import SshDestination

logger = logging.getLogger(__name__)


async def _relay_specs_by_agent(db: AsyncSession) -> dict[str, list[dict]]:
    result = await db.execute(select(SshDestination).where(SshDestination.active.is_(True)))
    destinations = result.scalars().all()
    specs: dict[str, list[dict]] = {}
    for destination in destinations:
        agent_key = destination.allowed_agent_id or ""
        specs.setdefault(agent_key, []).append(
            {"listen": destination.listen_port, "host": destination.host, "port": destination.port}
        )
    return specs


async def reconcile_ssh_relays(app: FastAPI, db: AsyncSession) -> None:
    client = getattr(app.state, "runtime_runner_client", None)
    if client is None:
        return
    specs = await _relay_specs_by_agent(db)
    from hermeshq.models.agent import Agent

    result = await db.execute(select(Agent.id))
    known_agents = [row for row in result.scalars().all()]
    for agent_id in known_agents:
        forwards = specs.get(agent_id, [])
        try:
            await client.push_ssh_relay(agent_id, forwards)
        except Exception:
            logger.exception("Failed to push ssh relay state for agent %s", agent_id)
