"""Per-token rate limiter using an in-memory sliding window.

This module provides a sliding-window rate limiter keyed by token ID.
Each token maintains a ``deque`` of request timestamps; requests older
than the configured window are evicted on every ``check`` call.  A
periodic ``cleanup`` is recommended to remove tokens that have gone
idle and prevent unbounded memory growth.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import HTTPException, status


class McpRateLimiter:
    """Sliding-window rate limiter backed by an in-memory store.

    The limiter tracks the number of requests made by each ``token_id``
    inside a rolling time window.  When the number of recorded requests
    equals or exceeds ``max_requests`` within ``window_seconds``, further
    requests are rejected with an HTTP 429 Too Many Requests response.

    Parameters
    ----------
    max_requests:
        Maximum number of requests allowed per token within the window.
    window_seconds:
        Width of the sliding window in seconds.

    Example
    -------
    >>> limiter = McpRateLimiter(max_requests=100, window_seconds=60)
    >>> await limiter.check("token-abc")  # succeeds
    >>> # … 100 more rapid calls …
    >>> await limiter.check("token-abc")  # raises HTTPException(429)
    """

    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # token_id -> deque of monotonic timestamps
        self._windows: defaultdict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, token_id: str) -> None:
        """Check whether *token_id* is within its rate budget.

        This method records a new request for *token_id* and raises if
        the token has exceeded its allowed request count within the
        current sliding window.

        Parameters
        ----------
        token_id:
            Identifier of the bearer token / client making the request.

        Raises
        ------
        HTTPException
            ``429 Too Many Requests`` when the rate limit is exceeded.
        """
        now = time.monotonic()
        cutoff = now - self._window_seconds

        async with self._lock:
            window = self._windows[token_id]

            # Evict timestamps that fell outside the sliding window.
            while window and window[0] <= cutoff:
                window.popleft()

            if len(window) >= self._max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded for token '{token_id}': "
                        f"maximum {self._max_requests} requests per "
                        f"{self._window_seconds}s window."
                    ),
                )

            # Record this request.
            window.append(now)

    def cleanup(self, max_age_seconds: int = 300) -> None:
        """Remove stale entries to prevent memory leaks.

        Tokens whose most-recent timestamp is older than
        ``max_age_seconds`` are removed entirely from the internal store.
        Call this periodically (e.g. via a background task) to bound
        memory usage.

        Parameters
        ----------
        max_age_seconds:
            Maximum age in seconds for the newest timestamp in a token's
            window.  Tokens whose entire window is older than this value
            are evicted.  Defaults to 300 seconds (5 minutes).
        """
        now = time.monotonic()
        cutoff = now - max_age_seconds

        # Build list of expired tokens to avoid mutating dict during iteration.
        expired_tokens = [
            token
            for token, window in self._windows.items()
            # Token is stale when its newest timestamp is older than the cutoff.
            if not window or window[-1] <= cutoff
        ]

        for token in expired_tokens:
            del self._windows[token]
