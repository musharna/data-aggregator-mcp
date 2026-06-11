"""Per-service async token-bucket rate limiter.

Paces outbound requests per upstream so we never trip a documented rate limit.
NCBI allows 3 req/s anonymously, 10 with an API key — a PER-ACCOUNT ceiling
shared across every eutils endpoint, so all ``NCBI*`` services draw from ONE
bucket. Buckets live at module level and persist across tool calls within the
long-lived stdio process. ``acquire`` is called inside ``_http._retrying`` so
every upstream request — and every retry — spends a token.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

_DEFAULT_RATE = 10.0


# Rate is sampled once at bucket-creation time (first request); a key added
# after the process starts requires a restart to take effect.
def _ncbi_rate() -> float:
    return 10.0 if os.environ.get("NCBI_API_KEY") else 3.0


class TokenBucket:
    """Classic token bucket. ``now``/``sleep`` are injectable for deterministic
    tests; a single lock serializes refill+consume so concurrent fan-out callers
    can't double-spend."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._now = now
        self._sleep = sleep
        self._updated = now()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._now()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)


_BUCKETS: dict[str, TokenBucket] = {}


def _bucket_for(service: str) -> str:
    return "ncbi" if service.startswith("NCBI") else "default"


def _rate_for(bucket: str) -> float:
    return _ncbi_rate() if bucket == "ncbi" else _DEFAULT_RATE


async def acquire(service: str) -> None:
    name = _bucket_for(service)
    bucket = _BUCKETS.get(name)
    if bucket is None:
        bucket = TokenBucket(_rate_for(name))
        _BUCKETS[name] = bucket
    await bucket.acquire()


def reset() -> None:
    """Clear all buckets (test isolation)."""
    _BUCKETS.clear()
