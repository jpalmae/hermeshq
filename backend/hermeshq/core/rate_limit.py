"""Sliding-window rate limiter for per-user or per-IP throttling."""

import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import HTTPException, status


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.time()
        recent = [t for t in self._buckets.get(key, []) if now - t < self._window]
        if recent:
            self._buckets[key] = recent
        elif key in self._buckets:
            del self._buckets[key]
        if len(self._buckets.get(key, [])) >= self._max:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": str(self._window)},
            )

    def record(self, key: str) -> None:
        self._buckets[key].append(time.time())

    def cleanup(self) -> None:
        now = time.time()
        stale = [k for k, v in self._buckets.items() if all(now - t >= self._window for t in v)]
        for k in stale:
            del self._buckets[k]

    def guard(self, key: str) -> Callable:
        def _done() -> None:
            self.record(key)
        self.check(key)
        return _done
