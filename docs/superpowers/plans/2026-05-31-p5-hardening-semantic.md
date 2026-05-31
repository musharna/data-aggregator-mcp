# P5 Hardening + Semantic Re-rank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make data-aggregator-mcp safe under real agent load (per-service rate pacing, response caching, health visibility) and add an optional semantic re-rank of search results — no fifth tool, no required API key or ML dependency.

**Architecture:** Four new single-responsibility modules wired into existing chokepoints. `_ratelimit.py` (token bucket) is acquired inside `_http._retrying`, so all three unbounded fan-outs are paced at once. `_cache.py` (TTL+LRU) backs `router.resolve` caching and replaces the unbounded `taxonomy._CACHE`. `health.py` powers an opt-in `list_sources(check_health=true)`. `embeddings.py` re-ranks the fetched search window via an optional OpenAI-compatible endpoint, fail-soft to keyword order. Module-level state persists across tool calls in the long-lived stdio process.

**Tech Stack:** Python 3, httpx (async), pytest, pytest_httpx. No new third-party dependency.

---

## Spec

Approved spec: `docs/superpowers/specs/2026-05-31-p5-hardening-semantic-design.md`.

## File Structure

- **Create** `src/data_aggregator_mcp/_ratelimit.py` — async token-bucket rate limiter + per-service registry.
- **Create** `src/data_aggregator_mcp/_cache.py` — `TTLCache` (TTL + LRU), `MISS` sentinel.
- **Create** `src/data_aggregator_mcp/health.py` — `probe_sources` upstream health checks.
- **Create** `src/data_aggregator_mcp/embeddings.py` — `embed`, `cosine_rank`, `rerank`.
- **Modify** `src/data_aggregator_mcp/_http.py` — acquire a rate token before each send.
- **Modify** `src/data_aggregator_mcp/taxonomy.py` — migrate `_CACHE` to `TTLCache`.
- **Modify** `src/data_aggregator_mcp/router.py` — resolve cache; `rank` param + rerank hook in `search_page`.
- **Modify** `src/data_aggregator_mcp/server.py` — `list_sources` `check_health`; `search` `rank` param.
- **Version**: `pyproject.toml`, `server.json` (×2), `__init__.py`, `tests/test_packaging.py`, `CHANGELOG.md`.

---

### Task 1: Token bucket (`_ratelimit.py`)

**Files:**

- Create: `src/data_aggregator_mcp/_ratelimit.py`
- Test: `tests/test_ratelimit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ratelimit.py
import asyncio

import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket, _bucket_for, _rate_for


class FakeClock:
    """Deterministic clock: sleep() advances virtual time instead of waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _reset():
    _ratelimit.reset()
    yield
    _ratelimit.reset()


@pytest.mark.asyncio
async def test_bucket_bursts_to_capacity_then_paces():
    clk = FakeClock()
    b = TokenBucket(rate=3.0, capacity=3.0, now=clk.now, sleep=clk.sleep)
    # First 3 acquires consume the initial burst at t=0.
    for _ in range(3):
        await b.acquire()
    assert clk.t == 0.0
    # 4th must wait 1/3s for one token to refill.
    await b.acquire()
    assert clk.t == pytest.approx(1 / 3, abs=1e-6)


def test_bucket_classifier_groups_all_ncbi():
    assert _bucket_for("NCBI esearch (geo)") == "ncbi"
    assert _bucket_for("NCBI efetch (sra)") == "ncbi"
    assert _bucket_for("NCBI idconv") == "ncbi"
    assert _bucket_for("Zenodo search") == "default"
    assert _bucket_for("EuropePMC search") == "default"


def test_ncbi_rate_responds_to_api_key(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _rate_for("ncbi") == 3.0
    monkeypatch.setenv("NCBI_API_KEY", "abc")
    assert _rate_for("ncbi") == 10.0


@pytest.mark.asyncio
async def test_acquire_shares_one_bucket_across_ncbi_services():
    await _ratelimit.acquire("NCBI esearch (geo)")
    await _ratelimit.acquire("NCBI efetch (sra)")
    assert list(_ratelimit._BUCKETS) == ["ncbi"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_ratelimit.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'data_aggregator_mcp._ratelimit'`).

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/_ratelimit.py
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


def _ncbi_rate() -> float:
    return 10.0 if (os.environ.get("NCBI_API_KEY") or os.environ.get("NCBI_EMAIL")) else 3.0


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_ratelimit.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/_ratelimit.py tests/test_ratelimit.py
git commit -m "feat(ratelimit): per-service async token-bucket limiter"
```

---

### Task 2: Wire the limiter into `_http._retrying`

**Files:**

- Modify: `src/data_aggregator_mcp/_http.py` (import + acquire before `client.request`)
- Test: `tests/test_http_ratelimit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_http_ratelimit.py
import httpx
import pytest

from data_aggregator_mcp import _http, _ratelimit


@pytest.mark.asyncio
async def test_request_acquires_a_token_per_send(httpx_mock, monkeypatch):
    calls: list[str] = []

    async def fake_acquire(service: str) -> None:
        calls.append(service)

    monkeypatch.setattr(_ratelimit, "acquire", fake_acquire)
    httpx_mock.add_response(url="https://example.test/x", json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _http.request_json(client, "GET", "https://example.test/x", service="Zenodo search")

    assert calls == ["Zenodo search"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_http_ratelimit.py -q`
Expected: FAIL (`calls == []` — no acquire wired yet).

- [ ] **Step 3: Add the import and the acquire call**

In `src/data_aggregator_mcp/_http.py`, add to the imports block (near the other `from data_aggregator_mcp...` imports):

```python
from data_aggregator_mcp import _ratelimit
```

Then inside `_retrying`, the attempt loop currently begins:

```python
    for attempt in range(max_retries):
        try:
            resp = await client.request(
                method, url, params=params, data=data, headers=headers, timeout=timeout
            )
```

Insert the token acquisition as the first statement inside the `try` (so a retry re-acquires):

```python
    for attempt in range(max_retries):
        try:
            await _ratelimit.acquire(service)
            resp = await client.request(
                method, url, params=params, data=data, headers=headers, timeout=timeout
            )
```

- [ ] **Step 4: Run to verify it passes, then the full suite**

Run: `python -m pytest tests/test_http_ratelimit.py -q`
Expected: PASS.
Run: `python -m pytest -q`
Expected: all pass (the real-clock limiter's generous default rate adds negligible delay; NCBI mocks fire ≤ capacity so no real sleep). If any existing test slows, it is firing > burst NCBI calls against mocks — acceptable, but confirm no failures.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/_http.py tests/test_http_ratelimit.py
git commit -m "feat(http): acquire a rate-limit token before every upstream send"
```

---

### Task 3: TTL+LRU cache (`_cache.py`)

**Files:**

- Create: `src/data_aggregator_mcp/_cache.py`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache.py
from data_aggregator_mcp._cache import MISS, TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def test_hit_within_ttl_then_miss_after_expiry():
    clk = FakeClock()
    c = TTLCache(maxsize=10, ttl=100.0, now=clk.now)
    c.set("k", "v")
    assert c.get("k") == "v"
    clk.t = 99.0
    assert c.get("k") == "v"
    clk.t = 100.0
    assert c.get("k") is MISS  # expired (>= expiry)


def test_lru_eviction_at_maxsize():
    c = TTLCache(maxsize=2, ttl=100.0)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1  # touch a -> a is now MRU
    c.set("c", 3)  # evicts LRU == b
    assert c.get("b") is MISS
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_ttl_zero_disables():
    c = TTLCache(maxsize=10, ttl=0.0)
    c.set("k", "v")
    assert c.get("k") is MISS


def test_stored_none_is_distinct_from_miss():
    c = TTLCache(maxsize=10, ttl=100.0)
    c.set("k", None)
    assert c.get("k") is None
    assert c.get("absent") is MISS
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cache.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/_cache.py
"""In-process TTL + LRU cache. Module-level instances persist across tool calls
in the long-lived stdio process. ``ttl <= 0`` disables it (get always misses,
set is a no-op). ``MISS`` is a sentinel distinct from a stored ``None``."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

MISS = object()


class TTLCache:
    def __init__(
        self,
        maxsize: int = 512,
        ttl: float = 3600.0,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._now = now
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any:
        if self.ttl <= 0:
            return MISS
        item = self._data.get(key)
        if item is None:
            return MISS
        expires, value = item
        if self._now() >= expires:
            del self._data[key]
            return MISS
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (self._now() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_cache.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/_cache.py tests/test_cache.py
git commit -m "feat(cache): TTL+LRU in-process cache"
```

---

### Task 4: Cache `router.resolve`

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (add cache + env TTL; wrap `resolve`)
- Test: `tests/test_router.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_router.py  (append)
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp.models import DataResource


@pytest.mark.asyncio
async def test_resolve_is_cached_by_id(monkeypatch):
    router._RESOLVE_CACHE.clear()
    calls = {"n": 0}

    async def fake_zenodo_resolve(client, rid):
        calls["n"] += 1
        return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="X")

    monkeypatch.setattr(router.zenodo, "resolve", fake_zenodo_resolve)
    first = await router.resolve(None, "zenodo:1")
    second = await router.resolve(None, "zenodo:1")
    assert calls["n"] == 1  # second served from cache
    assert first is second
```

(`client` is `None` because the patched resolve ignores it and no enrichment runs — the
returned resource has no `organism`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_router.py::test_resolve_is_cached_by_id -q`
Expected: FAIL (`AttributeError: module 'router' has no attribute '_RESOLVE_CACHE'`).

- [ ] **Step 3: Add the cache and wrap `resolve`**

In `src/data_aggregator_mcp/router.py`, add to imports:

```python
import os

from data_aggregator_mcp._cache import MISS, TTLCache
```

Add module-level cache (near the `_ADAPTERS` definition):

```python
def _cache_ttl() -> float:
    raw = os.environ.get("CACHE_TTL_SECONDS")
    if raw is None:
        return 3600.0
    try:
        return float(raw)
    except ValueError:
        return 3600.0


_RESOLVE_CACHE = TTLCache(maxsize=512, ttl=_cache_ttl())
```

Then in `resolve`, wrap the body. The current function starts:

```python
    rid = resource_id.strip()
    prefix = rid.split(":", 1)[0]
    if prefix in omics.PREFIXES:
```

Change the opening to check the cache first, and store before returning:

```python
    rid = resource_id.strip()
    cached = _RESOLVE_CACHE.get(rid)
    if cached is not MISS:
        return cached
    prefix = rid.split(":", 1)[0]
    if prefix in omics.PREFIXES:
```

And the function currently ends:

```python
    if resource.organism:
        try:
            resource = await _enrich_resource(client, resource)
        except Exception as exc:  # additive enrichment must not sink a valid resolve
            logger.warning("resolve enrichment failed for %s: %r", rid, exc)
    return resource
```

Change the final two lines to store then return:

```python
    if resource.organism:
        try:
            resource = await _enrich_resource(client, resource)
        except Exception as exc:  # additive enrichment must not sink a valid resolve
            logger.warning("resolve enrichment failed for %s: %r", rid, exc)
    _RESOLVE_CACHE.set(rid, resource)
    return resource
```

- [ ] **Step 4: Run to verify it passes, then the full suite**

Run: `python -m pytest tests/test_router.py -q`
Expected: PASS. (Other resolve tests that call twice could now hit the cache; if any existing test asserts a per-call side effect across two resolves of the same id, add `router._RESOLVE_CACHE.clear()` at its start — run the suite to find out.)
Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py tests/test_router.py
git commit -m "feat(router): cache resolve results by id (TTL, env-overridable)"
```

---

### Task 5: Migrate `taxonomy._CACHE` to `TTLCache`

**Files:**

- Modify: `src/data_aggregator_mcp/taxonomy.py`
- Test: `tests/test_taxonomy.py` (append; adjust any test that pokes the old dict)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_taxonomy.py  (append)
import pytest

from data_aggregator_mcp import taxonomy
from data_aggregator_mcp._cache import TTLCache


class _Clk:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_resolve_taxon_negative_cache_one_roundtrip(monkeypatch):
    taxonomy._CACHE.clear()
    calls = {"n": 0}

    async def fake_esearch(client, db, term, retmax=1):
        calls["n"] += 1
        return 0, []  # no ids -> negative

    monkeypatch.setattr(taxonomy._eutils, "esearch", fake_esearch)
    assert await taxonomy.resolve_taxon(None, "Nonexistus") is None
    assert await taxonomy.resolve_taxon(None, "Nonexistus") is None
    assert calls["n"] == 1  # negative result cached


@pytest.mark.asyncio
async def test_resolve_taxon_cache_expires(monkeypatch):
    clk = _Clk()
    taxonomy._CACHE = TTLCache(maxsize=64, ttl=10.0, now=clk.now)
    calls = {"n": 0}

    async def fake_esearch(client, db, term, retmax=1):
        calls["n"] += 1
        return 0, []

    monkeypatch.setattr(taxonomy._eutils, "esearch", fake_esearch)
    await taxonomy.resolve_taxon(None, "X")
    clk.t = 10.0  # expire
    await taxonomy.resolve_taxon(None, "X")
    assert calls["n"] == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_taxonomy.py -q`
Expected: FAIL (`_CACHE` is a plain `dict`, has no `.clear()` semantics matching TTLCache / no expiry; `.clear()` exists on dict so the negative-cache test may pass, but the expiry test fails because a dict never expires).

- [ ] **Step 3: Migrate the cache**

In `src/data_aggregator_mcp/taxonomy.py`, add import:

```python
from data_aggregator_mcp._cache import MISS, TTLCache
```

Replace:

```python
_CACHE: dict[str, TaxonInfo | None] = {}
```

with:

```python
_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)
```

Rewrite the cached lookups in `resolve_taxon`. Current body:

```python
    key = name.strip().lower()
    if not key:
        return None
    if key in _CACHE:
        return _CACHE[key]
    _count, ids = await _eutils.esearch(client, "taxonomy", name, retmax=1)
    if not ids:
        _CACHE[key] = None
        return None
    xml_text = await _eutils.efetch(client, "taxonomy", [ids[0]], retmode="xml")
    info = _parse_taxon(xml_text)
    _CACHE[key] = info
    return info
```

becomes:

```python
    key = name.strip().lower()
    if not key:
        return None
    cached = _CACHE.get(key)
    if cached is not MISS:
        return None if cached is _NEG else cached
    _count, ids = await _eutils.esearch(client, "taxonomy", name, retmax=1)
    if not ids:
        _CACHE.set(key, _NEG)
        return None
    xml_text = await _eutils.efetch(client, "taxonomy", [ids[0]], retmode="xml")
    info = _parse_taxon(xml_text)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
```

- [ ] **Step 4: Run to verify it passes, then the full suite**

Run: `python -m pytest tests/test_taxonomy.py -q`
Expected: PASS. (If a pre-existing test reads `_CACHE[key]` directly, change it to `_CACHE.get(key)` — run the suite to find any.)
Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/taxonomy.py tests/test_taxonomy.py
git commit -m "refactor(taxonomy): migrate unbounded _CACHE to shared TTLCache"
```

---

### Task 6: Health probe (`health.py`)

**Files:**

- Create: `src/data_aggregator_mcp/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health.py
import httpx
import pytest

from data_aggregator_mcp import health


@pytest.mark.asyncio
async def test_probe_one_up(httpx_mock):
    httpx_mock.add_response(url="https://up.test/", status_code=200)
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "zenodo", "https://up.test/")
    assert r["name"] == "zenodo"
    assert r["status"] == "up"
    assert isinstance(r["latency_ms"], int)
    assert r["detail"] is None


@pytest.mark.asyncio
async def test_probe_one_down_on_5xx(httpx_mock):
    httpx_mock.add_response(url="https://down.test/", status_code=503)
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "datacite", "https://down.test/")
    assert r["status"] == "down"
    assert "503" in r["detail"]


@pytest.mark.asyncio
async def test_probe_one_down_on_transport_error_never_raises(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "omics", "https://err.test/")
    assert r["status"] == "down"
    assert r["latency_ms"] is None
    assert r["detail"]


@pytest.mark.asyncio
async def test_probe_sources_covers_every_source(httpx_mock):
    httpx_mock.add_response(status_code=200)  # any URL
    async with httpx.AsyncClient() as client:
        results = await health.probe_sources(client)
    assert {r["name"] for r in results} == set(health._PROBE_TARGETS)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_health.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/health.py
"""Best-effort upstream health probe, folded into list_sources(check_health=true).

Each probe is a direct timed GET (NOT the _http retry path) so a down endpoint
reports fast, not after backoff retries. A probe NEVER raises — health is
observability, not a hard dependency. Probes do not acquire a rate-limit token
(infrequent, opt-in, one-shot)."""

from __future__ import annotations

import asyncio
import time

import httpx

_PROBE_TARGETS: dict[str, str] = {
    "zenodo": "https://zenodo.org/api/records?size=1",
    "datacite": "https://api.datacite.org/heartbeat",
    "omics": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
    "literature": (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        "?query=test&format=json&pageSize=1"
    ),
    "huggingface": "https://huggingface.co/api/datasets?limit=1",
}
_TIMEOUT = 5.0


async def _probe_one(client: httpx.AsyncClient, name: str, url: str) -> dict:
    start = time.monotonic()
    try:
        resp = await client.request("GET", url, timeout=_TIMEOUT)
    except Exception as exc:  # transport / timeout — report down, never raise
        return {"name": name, "status": "down", "latency_ms": None, "detail": repr(exc)[:200]}
    latency_ms = int((time.monotonic() - start) * 1000)
    if 200 <= resp.status_code < 400:
        return {"name": name, "status": "up", "latency_ms": latency_ms, "detail": None}
    return {
        "name": name,
        "status": "down",
        "latency_ms": latency_ms,
        "detail": f"HTTP {resp.status_code}",
    }


async def probe_sources(client: httpx.AsyncClient) -> list[dict]:
    names = list(_PROBE_TARGETS)
    results = await asyncio.gather(*(_probe_one(client, n, _PROBE_TARGETS[n]) for n in names))
    return list(results)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_health.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/health.py tests/test_health.py
git commit -m "feat(health): best-effort upstream probe_sources"
```

---

### Task 7: Fold health into `list_sources(check_health=true)`

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (import; `list_sources` tool inputSchema; dispatch)
- Test: `tests/test_server.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py  (append)
import pytest

from data_aggregator_mcp import server


@pytest.mark.asyncio
async def test_list_sources_default_is_network_free():
    out = await server._dispatch("list_sources", {})
    assert "sources" in out
    assert all("health" not in s for s in out["sources"])


@pytest.mark.asyncio
async def test_list_sources_check_health_merges_health(monkeypatch):
    async def fake_probe(client):
        return [
            {"name": n, "status": "up", "latency_ms": 12, "detail": None}
            for n in [s["name"] for s in server._SOURCES]
        ]

    monkeypatch.setattr(server.health_mod, "probe_sources", fake_probe)
    out = await server._dispatch("list_sources", {"check_health": True})
    assert all(s["health"]["status"] == "up" for s in out["sources"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_server.py -k list_sources -q`
Expected: FAIL (`server.health_mod` does not exist; `check_health` ignored).

- [ ] **Step 3: Wire it**

In `src/data_aggregator_mcp/server.py`, add to imports:

```python
from data_aggregator_mcp import health as health_mod
```

Find the `list_sources` `types.Tool(...)` definition (around line 311) and give it an inputSchema with the new optional param. The current tool likely has `inputSchema={"type": "object", "properties": {}}` (or similar). Set it to:

```python
        inputSchema={
            "type": "object",
            "properties": {
                "check_health": {
                    "type": "boolean",
                    "description": (
                        "When true, probe each source's base endpoint and attach a "
                        "'health' field ({status: up|down, latency_ms, detail}) to each "
                        "source. Default false: returns the static catalog with no network."
                    ),
                    "default": False,
                },
            },
        },
```

Then update the dispatch. The current handler is:

```python
    if name == "list_sources":
        return {"sources": _SOURCES}
```

Replace with:

```python
    if name == "list_sources":
        if not args.get("check_health"):
            return {"sources": _SOURCES}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            probed = {h["name"]: h for h in await health_mod.probe_sources(client)}
        return {"sources": [{**s, "health": probed.get(s["name"])} for s in _SOURCES]}
```

(`httpx` is already imported in server.py — confirm; it is used at line 334.)

- [ ] **Step 4: Run to verify it passes, then full suite**

Run: `python -m pytest tests/test_server.py -k list_sources -q`
Expected: PASS.
Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): opt-in list_sources(check_health) folds in upstream health"
```

---

### Task 8: Embeddings + cosine re-rank (`embeddings.py`)

**Files:**

- Create: `src/data_aggregator_mcp/embeddings.py`
- Test: `tests/test_embeddings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embeddings.py
import json

import httpx
import pytest

from data_aggregator_mcp import embeddings
from data_aggregator_mcp.models import DataResource


def test_cosine_rank_orders_by_similarity_zero_norm_last():
    q = [1.0, 0.0]
    cands = [[0.0, 1.0], [1.0, 0.0], [0.0, 0.0]]  # orthogonal, identical, zero
    assert embeddings.cosine_rank(q, cands) == [1, 0, 2]


@pytest.mark.asyncio
async def test_embed_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    async with httpx.AsyncClient() as client:
        assert await embeddings.embed(client, ["a", "b"]) is None


@pytest.mark.asyncio
async def test_embed_posts_and_parses(httpx_mock, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    httpx_mock.add_response(
        url="https://emb.test/v1/embeddings",
        json={"data": [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]}]},
    )
    async with httpx.AsyncClient() as client:
        vecs = await embeddings.embed(client, ["a", "b"])
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]
    req = httpx_mock.get_requests()[0]
    assert req.headers["authorization"] == "Bearer sk-x"
    assert json.loads(req.content) == {"model": "m", "input": ["a", "b"]}


@pytest.mark.asyncio
async def test_rerank_unconfigured_returns_unchanged_with_reason(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    rs = [
        DataResource(id="a", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="b", source="zenodo", kind="dataset", title="banana"),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "fruit", rs)
    assert out == rs
    assert reason is not None


@pytest.mark.asyncio
async def test_rerank_reorders_on_success(httpx_mock, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    # query vec, then two candidate vecs: 2nd candidate is the closer match.
    httpx_mock.add_response(
        url="https://emb.test/v1/embeddings",
        json={"data": [
            {"embedding": [0.0, 1.0]},   # query
            {"embedding": [1.0, 0.0]},   # cand 0 (orthogonal)
            {"embedding": [0.0, 1.0]},   # cand 1 (identical to query)
        ]},
    )
    rs = [
        DataResource(id="a", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="b", source="zenodo", kind="dataset", title="banana"),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "q", rs)
    assert reason is None
    assert [r.id for r in out] == ["b", "a"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_embeddings.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/embeddings.py
"""Optional semantic re-rank via a remote OpenAI-compatible embeddings endpoint.

Disabled (returns None) unless ``EMBEDDING_API_BASE`` is set. NEVER raises into
the search path — any failure degrades to keyword order. No local model, no
required key (a keyless local server is supported by omitting the auth header).

NOTE: ``_http`` sends bodies via httpx ``data=`` (no ``json=`` param), so we
serialize the JSON ourselves and set Content-Type explicitly.
"""

from __future__ import annotations

import json
import math
import os

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource

_MAX_CHARS = 2000


def _config() -> tuple[str, str | None, str] | None:
    base = os.environ.get("EMBEDDING_API_BASE")
    if not base:
        return None
    return (
        base.rstrip("/"),
        os.environ.get("EMBEDDING_API_KEY"),
        os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
    )


async def embed(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]] | None:
    cfg = _config()
    if cfg is None:
        return None
    base, key, model = cfg
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps({"model": model, "input": texts})
    try:
        body = await _http.request_json(
            client, "POST", f"{base}/embeddings", service="embeddings", data=payload, headers=headers
        )
        return [row["embedding"] for row in body["data"]]
    except Exception:  # unavailable / malformed — caller degrades to keyword order
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0  # zero-norm sorts last
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def cosine_rank(query_vec: list[float], cand_vecs: list[list[float]]) -> list[int]:
    """Indices of candidates sorted by descending cosine similarity to the query
    (ties broken by original order)."""
    scored = sorted(
        range(len(cand_vecs)),
        key=lambda i: (-_cosine(query_vec, cand_vecs[i]), i),
    )
    return scored


async def rerank(
    client: httpx.AsyncClient, query: str, resources: list[DataResource]
) -> tuple[list[DataResource], str | None]:
    """Re-order ``resources`` by semantic similarity to ``query``. On success
    returns ``(reordered, None)``; if embeddings are unavailable or fail, returns
    ``(resources_unchanged, reason)``."""
    if not resources:
        return resources, None
    texts = [f"{r.title or ''}\n{r.description or ''}"[:_MAX_CHARS] for r in resources]
    vecs = await embed(client, [query, *texts])
    if vecs is None or len(vecs) != len(resources) + 1:
        return resources, (
            "semantic re-rank unavailable (no embedding endpoint configured or embed failed)"
        )
    order = cosine_rank(vecs[0], vecs[1:])
    return [resources[i] for i in order], None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_embeddings.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/embeddings.py tests/test_embeddings.py
git commit -m "feat(embeddings): optional remote embeddings + cosine rerank (fail-soft)"
```

---

### Task 9: `rank` param on `search` (router + server)

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (`search_page` `rank` param, cursor, rerank hook)
- Modify: `src/data_aggregator_mcp/server.py` (`search` tool `rank` enum; dispatch passthrough)
- Test: `tests/test_router.py`, `tests/test_server.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_router.py  (append)
@pytest.mark.asyncio
async def test_search_semantic_reorders_window(monkeypatch):
    from data_aggregator_mcp.models import DataResource

    async def fake_zen_search(client, query, *, size, offset=0):
        return 2, [
            DataResource(id="zenodo:a", source="zenodo", kind="dataset", title="apple"),
            DataResource(id="zenodo:b", source="zenodo", kind="dataset", title="banana"),
        ]

    monkeypatch.setattr(router.zenodo, "search", fake_zen_search)

    async def fake_rerank(client, query, resources):
        return list(reversed(resources)), None  # deterministic reorder

    monkeypatch.setattr(router.embeddings, "rerank", fake_rerank)

    r = await router.search_page(
        None, query="fruit", size=10, sources=["zenodo"], rank="semantic"
    )
    assert [x.id for x in r.results] == ["zenodo:b", "zenodo:a"]


@pytest.mark.asyncio
async def test_search_semantic_failsoft_keeps_order_and_notes_error(monkeypatch):
    from data_aggregator_mcp.models import DataResource

    async def fake_zen_search(client, query, *, size, offset=0):
        return 1, [DataResource(id="zenodo:a", source="zenodo", kind="dataset", title="apple")]

    monkeypatch.setattr(router.zenodo, "search", fake_zen_search)

    async def fake_rerank(client, query, resources):
        return resources, "unavailable"

    monkeypatch.setattr(router.embeddings, "rerank", fake_rerank)

    r = await router.search_page(
        None, query="x", size=10, sources=["zenodo"], rank="semantic"
    )
    assert [x.id for x in r.results] == ["zenodo:a"]
    assert r.errors.get("semantic") == "unavailable"
```

```python
# tests/test_server.py  (append)
@pytest.mark.asyncio
async def test_search_dispatch_passes_rank(monkeypatch):
    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        from data_aggregator_mcp.models import SearchResult
        return SearchResult(query=kwargs.get("query"), total=0, count=0, results=[], errors={})

    monkeypatch.setattr(server.router, "search_page", fake_search_page)
    await server._dispatch("search", {"query": "q", "rank": "semantic"})
    assert captured["rank"] == "semantic"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_router.py -k semantic tests/test_server.py -k rank -q`
Expected: FAIL (`search_page` has no `rank` param / `embeddings` not imported in router; dispatch drops `rank`).

- [ ] **Step 3a: router — import + signature + cursor**

In `src/data_aggregator_mcp/router.py`, add to the package imports block (the `from data_aggregator_mcp import (...)` group):

```python
    embeddings,
```

Add `rank` to the `search_page` signature (after `cursor`):

```python
    cursor: str | None = None,
    rank: str = "relevance",
```

In the cursor-decode branch, read `rank` back so continuation pages keep the mode. The branch currently ends:

```python
        offsets = st["offsets"]
        expansion = None  # frozen on continuation; do not re-expand
        effective_query = query
        errors: dict[str, str] = {}
```

Add a line:

```python
        offsets = st["offsets"]
        rank = st.get("rank", "relevance")
        expansion = None  # frozen on continuation; do not re-expand
        effective_query = query
        errors: dict[str, str] = {}
```

- [ ] **Step 3b: router — rerank hook + window-based consume in semantic mode**

The function currently has, after the fan-out result loop:

```python
    merged = _dedup(interleave(per_source))

    emitted: list[DataResource] = []
    cut = -1
    for i, r in enumerate(merged):
        cut = i
        if _passes_filters(r, filters):
            emitted.append(r)
            if len(emitted) == size:
                break
    if cut < 0:
        cut = len(merged) - 1
    consumed = merged[: cut + 1]
```

Replace that whole block with a mode split:

```python
    merged = _dedup(interleave(per_source))

    if rank == "semantic":
        # Re-rank the full fetched window by semantic similarity, then emit the
        # top `size` that pass filters. Ranking needs every candidate, so the
        # WHOLE window is consumed (window-based pagination) — see the spec.
        reordered, reason = await embeddings.rerank(client, query, merged)
        if reason:
            errors["semantic"] = reason
        merged = reordered
        emitted = []
        for r in merged:
            if _passes_filters(r, filters):
                emitted.append(r)
                if len(emitted) == size:
                    break
        consumed = merged
        cut = len(merged) - 1
    else:
        emitted = []
        cut = -1
        for i, r in enumerate(merged):
            cut = i
            if _passes_filters(r, filters):
                emitted.append(r)
                if len(emitted) == size:
                    break
        if cut < 0:
            cut = len(merged) - 1
        consumed = merged[: cut + 1]
```

(`emitted: list[DataResource]` typing is now set inside both branches; remove the old standalone `emitted: list[DataResource] = []` / `cut = -1` lines that the block replaced.)

Then add `rank` to the cursor payload. The `_cursor.encode({...})` dict currently is:

```python
            {
                "q": query,
                "sources": sources,
                "organism": organism,
                "filters": filters,
                "size": size,
                "offsets": new_offsets,
            }
```

Add the `rank` key:

```python
            {
                "q": query,
                "sources": sources,
                "organism": organism,
                "filters": filters,
                "size": size,
                "offsets": new_offsets,
                "rank": rank,
            }
```

(The existing `more`/`new_offsets`/`consumed_per_adapter` tail is unchanged: in semantic mode `cut == len(merged) - 1`, so the `cut < len(merged) - 1` term is False and `more` is driven purely by the upstream totals — exactly the window-based advance the spec calls for.)

- [ ] **Step 3c: server — `rank` enum on the search tool + dispatch passthrough**

In `src/data_aggregator_mcp/server.py`, in the `search` tool's `inputSchema["properties"]`, add (next to `kind`):

```python
                "rank": {
                    "type": "string",
                    "enum": ["relevance", "semantic"],
                    "default": "relevance",
                    "description": (
                        "Result ordering. 'relevance' (default) = upstream/merged order. "
                        "'semantic' re-ranks the fetched page by embedding similarity to the "
                        "query (needs EMBEDDING_API_BASE; degrades to relevance order with an "
                        "errors['semantic'] note if unconfigured). In semantic mode pagination "
                        "is window-based (each page consumes its full fetched window)."
                    ),
                },
```

In `_dispatch`, the `"search"` case builds the kwargs for `router.search_page`. Add `rank` to that call. The current call passes `query`, `size`, `sources`, `organism`, `published_after`, `published_before`, `kind`, `cursor` from `args`. Add:

```python
                    rank=args.get("rank", "relevance"),
```

- [ ] **Step 4: Run to verify it passes, then full suite**

Run: `python -m pytest tests/test_router.py -k semantic -q && python -m pytest tests/test_server.py -k rank -q`
Expected: PASS.
Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py src/data_aggregator_mcp/server.py tests/test_router.py tests/test_server.py
git commit -m "feat(search): rank=semantic re-ranks the fetched window (fail-soft, window-paged)"
```

---

### Task 10: Real-execution probes

**Files:**

- Test: `tests/test_live_p5.py` (new; gated by `DATA_AGGREGATOR_MCP_LIVE`)

- [ ] **Step 1: Write the gated probes**

```python
# tests/test_live_p5.py
import os
import time

import httpx
import pytest

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 for live probes")


@pytest.mark.asyncio
async def test_live_health_probe_shape():
    from data_aggregator_mcp import health

    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await health.probe_sources(client)
    assert {r["name"] for r in results} == set(health._PROBE_TARGETS)
    for r in results:
        assert r["status"] in {"up", "down"}
        if r["status"] == "up":
            assert isinstance(r["latency_ms"], int)


@pytest.mark.asyncio
async def test_live_ncbi_rate_pacing():
    """6 real NCBI esearch calls on a 3/s bucket must span >= ~1s (real CLI path,
    not a mock) — the real-execution check that the limiter actually paces."""
    from data_aggregator_mcp import _eutils, _ratelimit

    _ratelimit.reset()
    async with httpx.AsyncClient() as client:
        start = time.monotonic()
        for _ in range(6):
            await _eutils.esearch(client, "pubmed", "cancer", retmax=1)
        elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # ~ (6 - capacity 3) / 3 s of forced pacing


@pytest.mark.asyncio
async def test_live_semantic_rerank_if_configured():
    if not os.environ.get("EMBEDDING_API_BASE"):
        pytest.skip("no EMBEDDING_API_BASE configured")
    from data_aggregator_mcp import embeddings
    from data_aggregator_mcp.models import DataResource

    rs = [
        DataResource(id="a", source="zenodo", kind="dataset", title="maize drought tolerance genomics"),
        DataResource(id="b", source="zenodo", kind="dataset", title="quantum chromodynamics lattice"),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "corn surviving dry conditions", rs)
    assert reason is None
    assert out[0].id == "a"  # the maize record ranks first
```

- [ ] **Step 2: Run (gated — passes trivially when LIVE unset)**

Run: `python -m pytest tests/test_live_p5.py -q`
Expected: all skipped (no `DATA_AGGREGATOR_MCP_LIVE`).
Optional live run: `DATA_AGGREGATOR_MCP_LIVE=1 python -m pytest tests/test_live_p5.py -q` → health + rate-pacing pass; semantic skips unless `EMBEDDING_API_BASE` set.

- [ ] **Step 3: Commit**

```bash
git add tests/test_live_p5.py
git commit -m "test: live P5 probes (health shape, NCBI rate pacing, semantic rerank)"
```

---

### Task 11: Version bump to 0.16.0

**Files:**

- Modify: `pyproject.toml:3`, `server.json:10` + `:16`, `src/data_aggregator_mcp/__init__.py:3`, `tests/test_packaging.py`, `CHANGELOG.md`

- [ ] **Step 1: Update `test_packaging.py` to expect 0.16.0 (failing first)**

In `tests/test_packaging.py`, change the three `0.15.0` asserts to `0.16.0`:

```python
    assert data_aggregator_mcp.__version__ == "0.16.0"
    assert _PYPROJECT["project"]["version"] == "0.16.0"
```

and

```python
    assert sj["version"] == "0.16.0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_packaging.py -q`
Expected: FAIL (sources still say 0.15.0).

- [ ] **Step 3: Bump the four synced source locations**

- `src/data_aggregator_mcp/__init__.py:3` → `__version__ = "0.16.0"`
- `pyproject.toml:3` → `version = "0.16.0"`
- `server.json:10` → `"version": "0.16.0",`
- `server.json:16` → `"version": "0.16.0",`

- [ ] **Step 4: Run to verify packaging passes**

Run: `python -m pytest tests/test_packaging.py -q`
Expected: PASS.

- [ ] **Step 5: Add the CHANGELOG section**

Prepend to `CHANGELOG.md` (above `## [0.15.0]`):

```markdown
## [0.16.0] - 2026-05-31

### Added

- Per-service rate limiting — an async token bucket paces outbound requests per
  upstream (NCBI 3/s, 10/s with `NCBI_API_KEY`/`NCBI_EMAIL`; generous elsewhere),
  acquired on every request and retry so a fan-out or 429-retry storm can't trip a
  documented limit.
- `list_sources(check_health=true)` — probes each source's base endpoint and
  attaches `{status, latency_ms, detail}` per source. The default call stays
  instant and network-free.
- `search(rank="semantic")` — re-ranks the fetched page by embedding similarity
  to the query via an optional OpenAI-compatible endpoint (`EMBEDDING_API_BASE`,
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`). Degrades to relevance order with an
  `errors["semantic"]` note when unconfigured or on failure. Semantic mode
  paginates window-by-window (each page consumes its full fetched window).

### Changed

- `resolve` results are cached in-process (TTL, default 3600s; `CACHE_TTL_SECONDS`
  to override, `0` disables). The previously unbounded taxonomy cache now uses the
  same bounded TTL+LRU cache.
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (skips for live + any no-key embedding tests).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml server.json src/data_aggregator_mcp/__init__.py tests/test_packaging.py CHANGELOG.md
git commit -m "chore: bump to 0.16.0 (P5 hardening + semantic re-rank)"
```

---

## Final Review

After all tasks: dispatch a whole-branch code review (feature-dev:code-reviewer), fix any
high-confidence findings, then use superpowers:finishing-a-development-branch to merge
`p5-hardening-semantic` into `main` with `git merge --no-ff` as v0.16.0.

## Self-Review Notes (author)

- **Spec coverage:** E3→T1+T2; F2→T3+T4+T5; F3→T6+T7; E4→T8+T9; real-execution→T10; version→T11. All spec sections mapped.
- **Type consistency:** `MISS` (shared sentinel) used in `_cache`, `router`, `taxonomy`. `TokenBucket(rate, capacity, *, now, sleep)`, `acquire(service)`, `reset()` consistent across T1/T2/T10. `embeddings.embed/cosine_rank/rerank` signatures consistent across T8/T9/T10. `search_page(..., rank="relevance")` consistent T9/T10.
- **`data=` not `json=`:** embeddings POSTs a serialized JSON string + explicit Content-Type because `_http._retrying` has no `json=` param (verified against live `_http.py`).
- **DataResource fields:** `rerank` reads `.title`/`.description` — both exist on the model (compact() sets `description`; title is required-ish). The implementer's first embeddings test constructs `DataResource(..., title=...)` and will surface any field mismatch immediately.
