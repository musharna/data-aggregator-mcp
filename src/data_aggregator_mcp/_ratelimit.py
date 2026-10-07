"""Per-upstream async rate limiting.

Paces outbound requests per upstream so we never trip a documented rate limit.
NCBI allows 3 req/s anonymously, 10 with an API key — a PER-ACCOUNT/IP ceiling
shared across every NCBI host, so all NCBI traffic draws from ONE schedule. The
bucket is chosen by request HOST rather than by service label, because the host
is what the upstream actually throttles. Buckets live at module level and persist
across tool calls within the long-lived stdio process. ``acquire`` is called
inside ``_http._retrying`` so every upstream request — and every retry — spends a
token.

The NCBI schedule is shared by every server process on the machine (a lock file
holding the next free slot), because NCBI counts the IP, not the process: three
processes each pacing themselves at 3/s sent 9/s, and every page of three
concurrent five-search runs lost a stream to HTTP 429, one 1,652 hits to 0
(2026-10-07). A user with several Claude sessions is three processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Windows: no flock, so NCBI is paced per process
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_RATE = 10.0
_EPS = 1e-9
# Where the shared schedules live; tests point it at a temporary directory so they
# never wait on, or delay, a live server on the same machine.
STATE_DIR_ENV = "DATA_AGGREGATOR_MCP_STATE_DIR"
# A next slot further ahead than this is a clock that jumped back or a corrupt file,
# not a queue: 30 s at 3/s is 90 requests waiting, more than all sessions send.
_MAX_AHEAD = 30.0


# Rate is sampled once at bucket-creation time (first request); a key added
# after the process starts requires a restart to take effect.
#
# Paced at two thirds of NCBI's documented ceiling (3/s, 10/s with a key), because
# arrival jitter bunches evenly spaced requests: one process spacing keyless requests
# exactly 1/3 s apart drew HTTP 429 on 3 to 4 of 18 in each of three trials, at 2.5/s on
# 1 of 54, at 2/s on none of 54 (2026-10-07). The keyed rate is the same fraction,
# unmeasured.
_NCBI_HEADROOM = 2 / 3


def _ncbi_rate() -> float:
    return (10.0 if os.environ.get("NCBI_API_KEY") else 3.0) * _NCBI_HEADROOM


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
        # One lock PER EVENT LOOP. The bucket is module-level and outlives any single
        # loop, but an asyncio.Lock binds to the loop of its first contended use, so a
        # single shared lock made the next ``asyncio.run`` raise RuntimeError. Token
        # state stays shared across loops — pacing is a property of the upstream.
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def acquire(self) -> None:
        async with self._lock():
            while True:
                now = self._now()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                # Epsilon: refill arithmetic lands on 0.9999999999999998 tokens, and the
                # remaining wait (~7e-17 s) is below a float clock's resolution at t~1 —
                # so without it the loop can sleep "forever" without time advancing.
                if self._tokens >= 1.0 - _EPS:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)


def state_dir() -> Path:
    """``$DATA_AGGREGATOR_MCP_STATE_DIR``, else the directory ``fetch`` downloads into."""
    if configured := os.environ.get(STATE_DIR_ENV):
        return Path(configured)
    return Path.home() / ".cache" / "data-aggregator-mcp"


class SharedSchedule:
    """Requests spaced ``1/rate`` apart across every process sharing ``path``, with no
    burst: NCBI's "3 requests per second" is broken by a token bucket's burst of 3
    followed by its refill. Each acquire claims the next free slot under an exclusive
    ``flock`` and sleeps until it.

    Where the file cannot be locked (no ``fcntl``, or an unwritable directory) it paces
    this process alone, without a burst, and logs why once."""

    def __init__(
        self,
        path: Path,
        rate: float,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.path = path
        self.rate = rate
        self._now = now  # wall clock: the one clock every process shares
        self._sleep = sleep
        self._local: TokenBucket | None = None if fcntl else self._per_process()

    def _per_process(self) -> TokenBucket:
        return TokenBucket(self.rate, 1.0, now=self._now, sleep=self._sleep)

    async def acquire(self) -> None:
        if self._local is None:
            try:
                wait = await asyncio.to_thread(self._claim)
            except OSError as exc:
                logger.warning(
                    "NCBI requests are paced in this process only: cannot lock %s (%s)",
                    self.path,
                    exc,
                )
                self._local = self._per_process()
            else:
                if wait > 0:
                    await self._sleep(wait)
                return
        await self._local.acquire()

    def _claim(self) -> float:
        """Take the next free slot; return how long to wait for it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                now = self._now()
                try:
                    free = float(f.read())
                except ValueError:  # first use, or a file that is not ours
                    free = now
                if free - now > _MAX_AHEAD:
                    free = now
                slot = max(now, free)
                f.seek(0)
                f.truncate()
                f.write(repr(slot + 1.0 / self.rate).encode())
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return slot - now


_BUCKETS: dict[str, TokenBucket | SharedSchedule] = {}


_NCBI_DOMAIN = "ncbi.nlm.nih.gov"


def _host_of(url: str) -> str:
    """Lowercase host of ``url``, or "" when it has none. Never raises — pacing must not
    be the thing that breaks a request."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _bucket_for(service: str, url: str) -> str:
    """Pick the token bucket for a request.

    Keyed on the HOST, because that is what the upstream throttles: NCBI's ceiling is per
    account/IP across all of its hosts, and our service label is only a display string for
    error messages. Keying on the label instead let ``GEO suppl listing`` — which fetches
    from ftp.ncbi.nlm.nih.gov — draw from the default bucket at 10 req/s, over three times
    NCBI's keyless ceiling, purely because of what the call was named.

    The label check is kept as a conservative backstop for a URL we cannot parse. It can
    only ever add pacing, never remove it.
    """
    host = _host_of(url)
    if host == _NCBI_DOMAIN or host.endswith("." + _NCBI_DOMAIN):
        return "ncbi"
    if host:
        return "default"
    return "ncbi" if service.startswith("NCBI") else "default"


def _rate_for(bucket: str) -> float:
    return _ncbi_rate() if bucket == "ncbi" else _DEFAULT_RATE


async def acquire(service: str, url: str) -> None:
    name = _bucket_for(service, url)
    bucket = _BUCKETS.get(name)
    if bucket is None:
        if name == "ncbi":
            bucket = SharedSchedule(state_dir() / "ncbi.schedule", _rate_for(name))
        else:
            bucket = TokenBucket(_rate_for(name))
        _BUCKETS[name] = bucket
    await bucket.acquire()


def reset() -> None:
    """Clear all buckets (test isolation)."""
    _BUCKETS.clear()
