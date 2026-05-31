# P1 — Pagination + Faceted Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cursor pagination (E1) + year/kind filters (E2) to `search`, ship v0.12.0.

**Architecture:** Each search adapter gains an `offset: int = 0` param (NCBI via `retstart`,
page-based APIs via `page = offset//size + 1` + tail-slice). A new `_cursor.py` encodes/decodes
the opaque token. `router.search` gains `cursor` + filter params, tracks per-adapter origin by
object identity, walks the deduped candidate list to a cut point (advancing past filter-rejected
records to avoid stalls), and sets `SearchResult.next_cursor`. `server.py` exposes the new args.

**Tech stack:** Python 3, httpx (async), pytest. Spec:
`docs/superpowers/specs/2026-05-30-p1-pagination-filters-design.md`.

**Conventions (verified live):** adapters return `(total, list[DataResource])`; `DEFAULT_SIZE=10`,
`MAX_SIZE=50` per adapter; tests use httpx mock transports in `tests/`; version is synced across
`pyproject.toml:3`, `src/data_aggregator_mcp/__init__.py:3`, `server.json:10` + `:16`. Post-edit a
formatter reflows files — re-Read before a follow-up Edit to the same region.

**Error taxonomy (verified live — IMPORTANT):** `errors.py` defines `DataAggregatorError`
(RuntimeError) + subclasses `RateLimitError`, `NotFoundError`, `UpstreamUnavailableError`,
`FetchTooLargeError`, `AuthRequiredError`, `FetchNotSupportedError`. **There is NO
`ValidationError`** — Task 2 adds `class ValidationError(DataAggregatorError)` to `errors.py`,
and every "fail loud" raise in this plan (corrupt cursor, unknown `kind`, neither `query` nor
`cursor`) uses it. `server._dispatch` has **no try/except** — raised exceptions propagate to the
MCP SDK, so raising is the correct fail-loud behavior.

---

### Task 1: `_eutils.esearch` gains `retstart`

**Files:**

- Modify: `src/data_aggregator_mcp/_eutils.py` (`esearch`, ~line 30)
- Test: `tests/test__eutils.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_esearch_sends_retstart():
    captured = {}
    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"esearchresult": {"count": "99", "idlist": ["1"]}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await _eutils.esearch(client, "pubmed", "cancer", retmax=10, retstart=20)
    assert captured["retstart"] == "20"

@pytest.mark.asyncio
async def test_esearch_default_retstart_zero_or_absent():
    captured = {}
    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["1"]}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await _eutils.esearch(client, "pubmed", "cancer", retmax=10)
    assert captured.get("retstart", "0") == "0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test__eutils.py -k retstart -v` — Expected: FAIL (unexpected keyword `retstart`).

- [ ] **Step 3: Implement**

In `esearch`, add keyword-only `retstart: int = 0` and include it:

```python
async def esearch(
    client: httpx.AsyncClient,
    db: str,
    term: str,
    *,
    retmax: int,
    retstart: int = 0,
) -> tuple[int, list[str]]:
    """Return (total_count, idlist) for ``term`` in NCBI database ``db``."""
    params = {
        "db": db,
        "term": term,
        "retmax": str(retmax),
        "retstart": str(retstart),
        **_common_params(),
    }
    # ...rest unchanged...
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test__eutils.py -v` — Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/_eutils.py tests/test__eutils.py
git commit -m "feat(eutils): esearch accepts retstart for row-offset paging"
```

---

### Task 2: `_cursor.py` — opaque token codec

**Files:**

- Create: `src/data_aggregator_mcp/_cursor.py`
- Test: `tests/test__cursor.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from data_aggregator_mcp import _cursor
from data_aggregator_mcp.errors import ValidationError


def test_cursor_roundtrip():
    state = {
        "q": "rice drought",
        "sources": ["zenodo", "datacite"],
        "organism": None,
        "filters": {"published_after": 2015, "published_before": None, "kind": "dataset"},
        "size": 10,
        "offsets": {"zenodo": 10, "datacite": 5},
    }
    token = _cursor.encode(state)
    assert isinstance(token, str)
    assert _cursor.decode(token) == state


def test_cursor_is_opaque_urlsafe():
    token = _cursor.encode({"q": "x", "size": 10, "offsets": {}})
    assert "/" not in token and "+" not in token  # urlsafe b64


@pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj", "{}"])
def test_cursor_decode_rejects_garbage(bad):
    with pytest.raises(ValidationError):
        _cursor.decode(bad)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test__cursor.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""Opaque, URL-safe pagination cursor: base64(json(state)). The token is never
promised to callers — only that a ``next_cursor`` from one search is replayable."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ValidationError

_REQUIRED = {"q", "size", "offsets"}


def encode(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        state = json.loads(raw)
    except Exception as exc:
        raise ValidationError(f"invalid or corrupt cursor: {exc}") from exc
    if not isinstance(state, dict) or not _REQUIRED.issubset(state):
        raise ValidationError("invalid or corrupt cursor: missing required fields")
    return state
```

**First** add the exception to `errors.py` (it does not exist yet), placing it with the other
subclasses:

```python
class ValidationError(DataAggregatorError):
    """Caller supplied invalid input (bad cursor, unknown filter value, ...)."""
```

Then import it in `_cursor.py` as shown.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test__cursor.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/_cursor.py tests/test__cursor.py
git commit -m "feat(cursor): opaque urlsafe-base64 pagination token codec"
```

---

### Task 3: `zenodo.search` gains `offset` (page emulation + tail slice)

**Files:**

- Modify: `src/data_aggregator_mcp/zenodo.py` (`search`, ~line 64)
- Test: `tests/test_zenodo.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_search_offset_requests_page_and_slices():
    captured = {}
    def make_record(i):
        return {"id": i, "metadata": {"title": f"r{i}", "publication_date": "2020-01-01"},
                "files": []}
    async def handler(request):
        captured.update(dict(request.url.params))
        recs = [make_record(i) for i in range(10)]
        return httpx.Response(200, json={"hits": {"total": 100, "hits": recs}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # offset=13, size=10 -> page 2 (offset//size+1), slice [13%10:] = drop first 3
        total, recs = await zenodo.search(client, "q", size=10, offset=13)
    assert captured["page"] == "2"
    assert captured["size"] == "10"
    assert len(recs) == 7  # 10 returned, sliced off first 3


@pytest.mark.asyncio
async def test_search_offset_zero_unchanged():
    captured = {}
    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": {"total": 0, "hits": []}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await zenodo.search(client, "q", size=10)
    assert captured.get("page", "1") == "1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_zenodo.py -k offset -v` — Expected: FAIL (unexpected keyword `offset`).

- [ ] **Step 3: Implement**

```python
async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Search Zenodo records. Returns (total_hits, COMPACT resources).

    ``offset`` selects the window [offset, offset+size): request page
    ``offset // size + 1`` at page-size ``size`` and drop the first
    ``offset % size`` records (page-boundary slice; see pagination spec).
    """
    capped = min(size, MAX_SIZE)
    params = {"q": query, "size": str(capped)}
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page"] = str(offset // capped + 1)
    data = await _http.request_json(
        client, "GET", f"{BASE_URL}/api/records",
        service="Zenodo search", params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT, max_retries=MAX_RETRIES,
    )
    hits = data.get("hits", {}) or {}
    records = hits.get("hits", []) or []
    sliced = records[offset % capped:]
    total = int(hits.get("total", len(records)))
    return total, [compact(_normalize(r)) for r in sliced]
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_zenodo.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/zenodo.py tests/test_zenodo.py
git commit -m "feat(zenodo): search offset via page+slice"
```

---

### Task 4: `datacite.search` gains `offset`

**Files:**

- Modify: `src/data_aggregator_mcp/datacite.py` (`search`, ~line 132)
- Test: `tests/test_datacite.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_search_offset_requests_page_number_and_slices():
    captured = {}
    def rec(i):
        return {"id": f"10.x/{i}", "type": "dois",
                "attributes": {"doi": f"10.x/{i}", "titles": [{"title": f"t{i}"}],
                               "publicationYear": 2020, "types": {}}}
    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": [rec(i) for i in range(10)],
                                         "meta": {"total": 100}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        total, recs = await datacite.search(client, "q", size=10, offset=20)
    assert captured["page[number]"] == "3"  # 20//10 + 1
    assert captured["page[size]"] == "10"
    assert len(recs) == 10  # 20%10 == 0, no slice
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_datacite.py -k offset -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

```python
async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Search DataCite DOIs. Returns (total_hits, COMPACT resources).
    ``offset`` → page ``offset // size + 1`` then drop first ``offset % size``."""
    capped = min(size, MAX_SIZE)
    params = {"query": query, "page[size]": str(capped)}
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page[number]"] = str(offset // capped + 1)
    body = await _http.request_json(
        client, "GET", f"{BASE_URL}/dois",
        service="DataCite search", params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT, max_retries=MAX_RETRIES,
    )
    items = (body.get("data", []) or [])[offset % capped:]
    total = int((body.get("meta") or {}).get("total", len(items)))
    return total, [compact(_normalize(it)) for it in items]
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_datacite.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/datacite.py tests/test_datacite.py
git commit -m "feat(datacite): search offset via page[number]+slice"
```

---

### Task 5: `openaire.search` gains `offset`

**Files:**

- Modify: `src/data_aggregator_mcp/openaire.py` (`search`, ~line 82)
- Test: `tests/test_openaire.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_search_offset_requests_page_and_slices():
    captured = {}
    async def handler(request):
        captured.update(dict(request.url.params))
        results = [{"id": str(i), "title": f"t{i}"} for i in range(10)]
        return httpx.Response(200, json={"header": {"numFound": 100}, "results": results})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        total, recs = await openaire.search(client, "q", size=10, offset=10)
    assert captured["page"] == "2"  # 10//10 + 1
    assert len(recs) == 10
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_openaire.py -k offset -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

Add `offset: int = 0`; compute `capped = min(size, MAX_SIZE)`. Add `"page"` to params **only when
`offset` is nonzero** (`params["page"] = offset // capped + 1`), so the `offset=0` request stays
byte-identical to today's (existing exact-URL mocks must stay green — see the eutils `retstart`
precedent commit `eb5f098`). Slice results `[offset % capped:]` before normalizing. Match the
existing `_normalize_openaire` call. The OpenAIRE param is 1-indexed `page` alongside `pageSize`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_openaire.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/openaire.py tests/test_openaire.py
git commit -m "feat(openaire): search offset via page+slice"
```

---

### Task 6: `omics.search` threads `offset` to each db

**Files:**

- Modify: `src/data_aggregator_mcp/omics.py` (`_search_db` ~line 107, `search` ~line 128)
- Test: `tests/test_omics.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_search_offset_threads_retstart(monkeypatch):
    seen = []
    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        seen.append((db, retstart))
        return 0, []
    monkeypatch.setattr(omics._eutils, "esearch", fake_esearch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        await omics.search(client, "q", size=10, offset=30)
    assert all(rs == 30 for _, rs in seen)
    assert len(seen) == len(omics._DB)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_omics.py -k offset -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

Add `offset: int = 0` to `_search_db` and `search`. In `_search_db`, pass `retstart=offset` to
`_eutils.esearch`. In `search`, pass `offset=offset` to each `_search_db(...)`. Keep the existing
`interleave(per_db)[:capped]` — note each db is paged by the same offset.

```python
async def _search_db(client, db, query, size, offset=0):
    count, ids = await _eutils.esearch(client, db, query, retmax=size, retstart=offset)
    ...

async def search(client, query, *, size=DEFAULT_SIZE, offset=0):
    capped = min(size, MAX_SIZE)
    outcomes = await asyncio.gather(
        *(_search_db(client, db, query, capped, offset) for db in _DB.values()),
        return_exceptions=True,
    )
    ...
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_omics.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/omics.py tests/test_omics.py
git commit -m "feat(omics): thread offset to esearch retstart per db"
```

---

### Task 7: `pubmed.search` + `literature.search` thread `offset`

**Files:**

- Modify: `src/data_aggregator_mcp/pubmed.py` (`search`, ~line 75)
- Modify: `src/data_aggregator_mcp/literature.py` (`search`, ~line 28)
- Test: `tests/test_pubmed.py`, `tests/test_literature.py`

- [ ] **Step 1: Write the failing tests**

```python
# test_pubmed.py
@pytest.mark.asyncio
async def test_search_offset_threads_retstart(monkeypatch):
    seen = {}
    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        seen["retstart"] = retstart
        return 0, []
    monkeypatch.setattr(pubmed._eutils, "esearch", fake_esearch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        await pubmed.search(client, "q", size=10, offset=25)
    assert seen["retstart"] == 25

# test_literature.py
@pytest.mark.asyncio
async def test_search_offset_forwarded_to_backends(monkeypatch):
    seen = []
    async def fake_backend_search(client, query, *, size, offset=0):
        seen.append(offset)
        return 0, []
    for b in literature._BACKENDS.values():
        monkeypatch.setattr(b, "search", fake_backend_search)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        await literature.search(client, "q", size=10, offset=40)
    assert seen and all(o == 40 for o in seen)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_pubmed.py tests/test_literature.py -k offset -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

`pubmed.search`: add `offset: int = 0`, pass `retstart=offset` to `_eutils.esearch`.
`literature.search`: add `offset: int = 0`, pass `offset=offset` into each backend
`b.search(client, query, size=capped, offset=offset)`. Keep `interleave(per_backend)[:capped]`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_pubmed.py tests/test_literature.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/pubmed.py src/data_aggregator_mcp/literature.py tests/test_pubmed.py tests/test_literature.py
git commit -m "feat(literature): thread offset through pubmed/openaire backends"
```

---

### Task 8: `router.search` — cursor, filters, cut-point offset advance

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (`search`, ~line 152; add a `_passes_filters` helper
  - a `_FILTERS` typed dict-ish; import `_cursor`)
- Test: `tests/test_router.py`

This is the core task. Implement the spec's router algorithm (steps 1–10) exactly.

- [ ] **Step 1: Write the failing tests** (each adapter mocked via `monkeypatch` on
      `router._ADAPTERS[name].search`; helper builds `DataResource`s with controllable doi/year/kind)

```python
def _res(rid, *, doi=None, year=2020, kind="dataset", source="zenodo"):
    from data_aggregator_mcp.models import DataResource
    return DataResource(id=rid, source=source, kind=kind, title=rid, doi=doi, year=year)

def _mock_adapter(monkeypatch, name, pages):
    """pages: dict offset -> (total, [DataResource]). search() looks up by offset."""
    async def search(client, query, *, size, offset=0):
        return pages.get(offset, (0, []))
    monkeypatch.setattr(router._ADAPTERS[name], "search", search)

@pytest.mark.asyncio
async def test_fresh_search_sets_next_cursor_when_full_window(monkeypatch):
    _mock_adapter(monkeypatch, "zenodo", {0: (100, [_res(f"z{i}", doi=f"10.z/{i}") for i in range(10)])})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        total, results, errors, _ = await router.search(client, "q", size=10)
    # next_cursor is exposed via the server layer; router returns offsets implicitly.
    # Assert via the dedicated paginated entrypoint (see Step 3 for return-shape decision).

@pytest.mark.asyncio
async def test_continuation_advances_offsets(monkeypatch):
    _mock_adapter(monkeypatch, "zenodo", {
        0: (100, [_res(f"z{i}", doi=f"10.z/{i}") for i in range(10)]),
        10: (100, [_res(f"z{i}", doi=f"10.z/{i}") for i in range(10, 20)]),
    })
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, []), 10: (0, [])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        p1 = await router.search_page(client, query="q", size=10)
        assert p1.next_cursor is not None
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    ids1 = {r.id for r in p1.results}
    ids2 = {r.id for r in p2.results}
    assert ids1.isdisjoint(ids2)  # page 2 walked deeper

@pytest.mark.asyncio
async def test_filter_stall_is_avoided(monkeypatch):
    # all page-1 records are year=1999 (filtered out by published_after=2010);
    # page-2 records pass. Offsets MUST advance past the rejected page.
    _mock_adapter(monkeypatch, "zenodo", {
        0: (100, [_res(f"z{i}", doi=f"10.z/{i}", year=1999) for i in range(10)]),
        10: (100, [_res(f"z{i}", doi=f"10.z/{i}", year=2015) for i in range(10, 20)]),
    })
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, []), 10: (0, [])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        p1 = await router.search_page(client, query="q", size=10, published_after=2010)
    assert p1.results == [] and p1.next_cursor is not None  # advanced, not stalled

@pytest.mark.asyncio
async def test_year_and_kind_filters(monkeypatch):
    recs = [_res("a", doi="10/a", year=2005, kind="dataset"),
            _res("b", doi="10/b", year=2018, kind="dataset"),
            _res("c", doi="10/c", year=2018, kind="publication"),
            _res("d", doi="10/d", year=None, kind="dataset")]
    _mock_adapter(monkeypatch, "zenodo", {0: (4, recs)})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        p = await router.search_page(client, query="q", size=10,
                                     published_after=2010, kind="dataset")
    assert {r.id for r in p.results} == {"b"}  # 2018 dataset only; c wrong kind, a too old, d year=None dropped

@pytest.mark.asyncio
async def test_unknown_kind_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValidationError):
            await router.search_page(client, query="q", kind="nonsense")

@pytest.mark.asyncio
async def test_corrupt_cursor_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValidationError):
            await router.search_page(client, cursor="garbage!!")

@pytest.mark.asyncio
async def test_page1_unfiltered_matches_legacy(monkeypatch):
    recs = [_res(f"z{i}", doi=f"10.z/{i}") for i in range(5)]
    _mock_adapter(monkeypatch, "zenodo", {0: (5, recs)})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        legacy = await router.search(client, "q", size=10)       # old 4-tuple
        page = await router.search_page(client, query="q", size=10)
    assert [r.id for r in legacy[1]] == [r.id for r in page.results]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_router.py -k "cursor or filter or page or stall or continuation" -v` —
Expected: FAIL (`search_page` missing).

- [ ] **Step 3: Implement**

Add a new `search_page` coroutine that returns a `SearchResult` (so `next_cursor` rides the model),
keeping the legacy 4-tuple `search` as a thin wrapper for backward-compat callers/tests.

```python
from . import _cursor

_VALID_KINDS = {"dataset", "sequencing_run", "study", "publication", "software"}


def _passes_filters(r: DataResource, f: dict) -> bool:
    pa, pb, kind = f.get("published_after"), f.get("published_before"), f.get("kind")
    if kind is not None and r.kind != kind:
        return False
    if (pa is not None or pb is not None) and r.year is None:
        return False
    if pa is not None and (r.year is None or r.year < pa):
        return False
    if pb is not None and (r.year is None or r.year > pb):
        return False
    return True


async def search_page(
    client: httpx.AsyncClient,
    *,
    query: str | None = None,
    size: int = 10,
    sources: list[str] | None = None,
    organism: str | None = None,
    published_after: int | None = None,
    published_before: int | None = None,
    kind: str | None = None,
    cursor: str | None = None,
) -> SearchResult:
    if kind is not None and kind not in _VALID_KINDS:
        raise ValidationError(f"unknown kind {kind!r}; valid: {sorted(_VALID_KINDS)}")

    if cursor is not None:
        st = _cursor.decode(cursor)
        query = st["q"]; sources = st.get("sources"); organism = st.get("organism")
        filters = st.get("filters") or {}; size = st["size"]; offsets = st["offsets"]
        expansion = None  # frozen; do not re-expand
        effective_query = query
        errors: dict[str, str] = {}
    else:
        if query is None:
            raise ValidationError("search requires either 'query' or 'cursor'")
        filters = {"published_after": published_after,
                   "published_before": published_before, "kind": kind}
        errors = {}
        effective_query, expansion = await _expand_organism(client, query, organism, errors)
        offsets = {}

    adapters = _select(sources)
    names = list(adapters)
    outcomes = await asyncio.gather(
        *(adapters[n].search(client, effective_query, size=size, offset=offsets.get(n, 0))
          for n in names),
        return_exceptions=True,
    )

    origin: dict[int, str] = {}
    per_source: list[list[DataResource]] = []
    returned_full: dict[str, bool] = {}
    total = 0
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, Exception):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            returned_full[name] = False
            continue
        adapter_total, recs = outcome
        total += adapter_total
        returned_full[name] = len(recs) == size
        for r in recs:
            origin[id(r)] = name
        per_source.append(recs)

    merged = _dedup(interleave(per_source))

    emitted: list[DataResource] = []
    cut = -1
    for i, r in enumerate(merged):
        if _passes_filters(r, filters):
            emitted.append(r)
            cut = i
            if len(emitted) == size:
                break
        else:
            cut = i
    if cut < 0:
        cut = len(merged) - 1
    consumed = merged[: cut + 1]

    from collections import Counter
    consumed_per_adapter = Counter(origin[id(r)] for r in consumed)
    new_offsets = {n: offsets.get(n, 0) + consumed_per_adapter.get(n, 0) for n in names}

    more = (cut < len(merged) - 1) or any(returned_full.get(n, False) for n in names)
    next_cursor = _cursor.encode({
        "q": query, "sources": sources, "organism": organism,
        "filters": filters, "size": size, "offsets": new_offsets,
    }) if more else None

    enriched = await _enrich(client, emitted, errors)
    return SearchResult(
        query=query, total=total, count=len(enriched), results=enriched,
        errors=errors, next_cursor=next_cursor, taxon_expansion=expansion,
    )
```

Verify the exact `SearchResult(...)` constructor field names against `models.py` before
finalizing (e.g. `taxon_expansion` vs `taxonExpansion`; `count` vs derived). Keep the legacy
`search` returning its 4-tuple by delegating:

```python
async def search(client, query, *, size=10, sources=None, organism=None):
    r = await search_page(client, query=query, size=size, sources=sources, organism=organism)
    return r.total, r.results, r.errors, r.taxon_expansion
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_router.py -v` — Expected: PASS (incl. legacy tests untouched).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py tests/test_router.py
git commit -m "feat(router): cursor pagination + year/kind filters via search_page"
```

---

### Task 9: `server.py` — expose `cursor` + filter args on the `search` tool

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (`search` tool `inputSchema` + `_dispatch`)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

```python
def test_search_schema_exposes_pagination_and_filters():
    schema = _search_tool_input_schema()  # however the test already reaches the schema
    props = schema["properties"]
    assert {"cursor", "published_after", "published_before", "kind"} <= set(props)
    assert props["kind"]["enum"] == ["dataset", "sequencing_run", "study", "publication", "software"]

@pytest.mark.asyncio
async def test_dispatch_threads_cursor(monkeypatch):
    captured = {}
    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query="q", total=0, count=0, results=[], errors={})
    monkeypatch.setattr(router, "search_page", fake_search_page)
    await _dispatch("search", {"cursor": "tok"})  # match the real dispatch entrypoint
    assert captured["cursor"] == "tok"
```

(Match the test names/entrypoints already used in `tests/test_server.py` — read it first.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_server.py -k "schema or cursor or filter" -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

In the `search` tool definition, add to `inputSchema.properties`:

```python
"cursor": {"type": "string",
           "description": "Opaque pagination token from a prior search's next_cursor. "
                          "When set, all other search params are read from the cursor."},
"published_after": {"type": "integer", "description": "Keep results with year >= this."},
"published_before": {"type": "integer", "description": "Keep results with year <= this."},
"kind": {"type": "string",
         "enum": ["dataset", "sequencing_run", "study", "publication", "software"],
         "description": "Keep only results of this kind."},
```

Make `query` not strictly required when `cursor` is present (relax `required`, validate in
router). Replace the current `"search"` case body — which calls `router.search(...)` then
**rebuilds** `SearchResult(query=args["query"], ...)` (a `KeyError` when only `cursor` is given)
— with a direct call that returns `search_page`'s model:

```python
case "search":
    result = await router.search_page(
        client,
        query=args.get("query"),
        size=args.get("size", zenodo.DEFAULT_SIZE),
        sources=args.get("sources"),
        organism=args.get("organism"),
        published_after=args.get("published_after"),
        published_before=args.get("published_before"),
        kind=args.get("kind"),
        cursor=args.get("cursor"),
    )
    return result.model_dump()
```

`search_page` already builds the `SearchResult` (carrying `next_cursor` + `query` from the
cursor), so no reconstruction is needed.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_server.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): expose cursor + year/kind filters on search tool"
```

---

### Task 10: `list_sources` advertises the new capabilities

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (`list_sources` entries' `filters_supported`)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

```python
def test_list_sources_advertises_filters_and_cursor():
    sources = _list_sources_payload()  # match the real accessor
    for s in sources:
        assert {"published_after", "published_before", "kind", "cursor"} <= set(s["filters_supported"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_server.py -k list_sources -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

Append `"published_after"`, `"published_before"`, `"kind"`, `"cursor"` to each source entry's
`filters_supported` list.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_server.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(list_sources): advertise pagination + filter support"
```

---

### Task 11: Version bump to 0.12.0 + CHANGELOG

**Files:**

- Modify: `pyproject.toml:3`, `src/data_aggregator_mcp/__init__.py:3`, `server.json:10` + `:16`
- Modify: `CHANGELOG.md`, `tests/test_packaging.py`

- [ ] **Step 1: Update the version test**

In `tests/test_packaging.py` bump the asserted version to `0.12.0`.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_packaging.py -v` — Expected: FAIL (still 0.11.0).

- [ ] **Step 3: Bump all four synced places**

Set version `0.12.0` in `pyproject.toml`, `__init__.py`, both `server.json` occurrences.

- [ ] **Step 4: Prepend a CHANGELOG section**

```markdown
## [0.12.0] - 2026-05-30

### Added

- `search` pagination — an opaque `next_cursor` walks past the first page of merged
  results; pass it back as `cursor` to fetch the next page (per-source offsets, `size`
  stays "deduped results per page").
- `search` filters — `published_after` / `published_before` (year bounds) and `kind`
  constrain results (post-fetch on normalized fields; see spec for the window caveat).
- `list_sources` now advertises `published_after`/`published_before`/`kind`/`cursor`.
```

- [ ] **Step 5: Run full suite + commit**

Run: `pytest -q` — Expected: PASS (all).

```bash
git add pyproject.toml src/data_aggregator_mcp/__init__.py server.json CHANGELOG.md tests/test_packaging.py
git commit -m "chore: bump to 0.12.0 (pagination + filters)"
```

---

### Task 12: Real-execution pagination probe (boundary)

**Files:**

- Test: `tests/test_workflows.py` (or wherever the repo's live/network probes live — read first)

- [ ] **Step 1: Write a network-gated probe**

```python
@pytest.mark.asyncio
@pytest.mark.live  # match the repo's existing live-probe marker / skip-when-offline guard
async def test_live_zenodo_datacite_pagination():
    async with httpx.AsyncClient() as client:
        p1 = await router.search_page(client, query="climate", size=5, sources=["zenodo", "datacite"])
        assert p1.next_cursor
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    assert {r.id for r in p1.results}.isdisjoint({r.id for r in p2.results})
```

Use the repo's established live-test gating (env flag / marker) so the default suite stays offline.

- [ ] **Step 2: Run it explicitly**

Run (network): `<repo's live-test invocation> -k live_zenodo_datacite_pagination -v` —
Expected: PASS against the real APIs (page 2 differs from page 1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_workflows.py
git commit -m "test: live pagination probe across zenodo+datacite"
```

---

## Final review

After all 12 tasks: run `pytest -q` (full suite green), then dispatch a whole-branch code review,
then use `superpowers:finishing-a-development-branch` to merge and ship v0.12.0.
