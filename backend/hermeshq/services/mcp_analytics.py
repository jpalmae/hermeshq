"""MCP usage analytics — tracks request counts, latency, errors per token."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.activity import ActivityLog


@dataclass
class _Bucket:
    """Sliding-window bucket for one token × method."""
    total: int = 0
    errors: int = 0
    latency_sum: float = 0.0
    last_ts: float = 0.0


@dataclass
class McpAnalytics:
    """In-memory MCP usage tracker.

    Provides:
    - Per-token request counts, error rates, avg latency
    - Global stats (total requests, unique tokens)
    - Top methods, top tokens
    - Persistent stats via ActivityLog queries
    """

    # Per-token → method → bucket
    _buckets: dict[str, dict[str, _Bucket]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(_Bucket)),
    )
    # Global counters
    _total_requests: int = 0
    _total_errors: int = 0
    _start_time: float = field(default_factory=time.monotonic)

    # ── Recording ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        token_id: str,
        method: str,
        latency_ms: float,
        error: bool = False,
    ) -> None:
        b = self._buckets[token_id][method]
        b.total += 1
        b.latency_sum += latency_ms
        b.last_ts = time.monotonic()
        if error:
            b.errors += 1
            self._total_errors += 1
        self._total_requests += 1

    # ── Live stats ───────────────────────────────────────────────────────

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def global_stats(self) -> dict[str, Any]:
        unique_tokens = len(self._buckets)
        methods: dict[str, int] = {}
        for token_methods in self._buckets.values():
            for m, b in token_methods.items():
                methods[m] = methods.get(m, 0) + b.total
        return {
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "unique_tokens": unique_tokens,
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "methods": methods,
        }

    def token_stats(self, token_id: str) -> dict[str, Any]:
        methods = self._buckets.get(token_id, {})
        total = sum(b.total for b in methods.values())
        errors = sum(b.errors for b in methods.values())
        latency = sum(b.latency_sum for b in methods.values())
        return {
            "token_id": token_id,
            "total_requests": total,
            "total_errors": errors,
            "avg_latency_ms": round(latency / total, 2) if total else 0,
            "methods": {
                m: {
                    "calls": b.total,
                    "errors": b.errors,
                    "avg_latency_ms": round(b.latency_sum / b.total, 2) if b.total else 0,
                }
                for m, b in methods.items()
            },
        }

    def top_tokens(self, limit: int = 10) -> list[dict[str, Any]]:
        token_totals = [
            (tid, sum(b.total for b in methods.values()))
            for tid, methods in self._buckets.items()
        ]
        token_totals.sort(key=lambda x: x[1], reverse=True)
        return [
            {"token_id": tid, "total_requests": cnt}
            for tid, cnt in token_totals[:limit]
        ]

    # ── Persistent stats from ActivityLog ────────────────────────────────

    @staticmethod
    async def persistent_stats(
        db: AsyncSession,
        hours: int = 24,
    ) -> dict[str, Any]:
        """Query ActivityLog for MCP events in the last N hours."""
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Total MCP events
        total_q = await db.execute(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.event_type.startswith("mcp."),
                ActivityLog.created_at >= since,
            )
        )
        total = total_q.scalar_one()

        # By event type
        type_q = await db.execute(
            select(ActivityLog.event_type, func.count(ActivityLog.id))
            .where(ActivityLog.event_type.startswith("mcp."), ActivityLog.created_at >= since)
            .group_by(ActivityLog.event_type)
            .order_by(func.count(ActivityLog.id).desc())
        )
        by_type = {row[0]: row[1] for row in type_q.all()}

        # By token — use a labeled expression for GROUP BY compatibility
        token_name_expr = ActivityLog.details["mcp_access_token_name"].as_string().label("token_name")
        token_q = await db.execute(
            select(
                token_name_expr,
                func.count(ActivityLog.id),
            )
            .where(ActivityLog.event_type.startswith("mcp."), ActivityLog.created_at >= since)
            .group_by(token_name_expr)
            .order_by(func.count(ActivityLog.id).desc())
            .limit(10)
        )
        by_token = {row[0] or "unknown": row[1] for row in token_q.all()}

        return {
            "period_hours": hours,
            "total_events": total,
            "by_event_type": by_type,
            "by_token_name": by_token,
        }
