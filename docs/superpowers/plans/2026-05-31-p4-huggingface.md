# P4 — HuggingFace Datasets Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox
> steps. Spec: `docs/superpowers/specs/2026-05-31-p4-huggingface-design.md`.

**Goal:** Add HuggingFace datasets as a 5th source (search/resolve/fetch); ship v0.15.0.

**Architecture:** New `huggingface.py` adapter (search/resolve/\_normalize) mirroring `zenodo.py`;
register in `router._ADAPTERS` + resolve routing; gate fetch + advertise in `list_sources`.

**Conventions (verified live):** search adapter contract = `async search(client, query, *, size,
offset=0) -> tuple[int, list[DataResource]]` (COMPACT) + `async resolve(client, id) ->
DataResource` + module `PREFIXES`/`DEFAULT_SIZE`/`MAX_SIZE`. Models: `DataResource`, `FileEntry`,
`Creator`, `compact` in `models.py`; errors in `errors.py` (`NotFoundError`). `_http.request_json`
is the shared GET helper (see how zenodo uses it: `service=`, `params=`, `headers=`, `timeout=`,
`max_retries=`). PostToolUse formatter reflows after each write — re-Read before a 2nd Edit to the
same region; fresh-read-guard blocks unread-file edits; add imports in the same edit as first
usage (ruff strips unused-then-used). Commit trailer (exactly): `Co-Authored-By: Claude Opus 4.8
(1M context) <noreply@anthropic.com>`.

**Live API shapes (grounded 2026-05-31):**

- search: `GET https://huggingface.co/api/datasets?search=<q>&limit=<n>&full=true` → list of
  `{id:"owner/name", author, createdAt, tags:[...], gated, siblings?}`.
- resolve: `GET https://huggingface.co/api/datasets/<id>?full=true` → adds `siblings:[{rfilename}]`,
  sometimes `cardData`.
- file URL: `https://huggingface.co/datasets/<id>/resolve/main/<rfilename>`.

---

### Task 1: `huggingface.py` adapter

**Files:** Create `src/data_aggregator_mcp/huggingface.py`; create `tests/test_huggingface.py`.
Read `src/data_aggregator_mcp/zenodo.py` first as the structural template.

- [ ] **Step 1: failing tests** (`tests/test_huggingface.py`, httpx `MockTransport`)

```python
import httpx, pytest
from data_aggregator_mcp import huggingface
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

_DS = {"id": "owner/name", "author": "owner", "createdAt": "2022-06-09T17:34:13.000Z",
       "tags": ["license:mit", "format:parquet", "biology"], "gated": False}


@pytest.mark.asyncio
async def test_search_normalizes():
    async def handler(request):
        assert request.url.params["search"] == "dna"
        return httpx.Response(200, json=[_DS])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await huggingface.search(c, "dna", size=10)
    assert total == 1
    r = recs[0]
    assert r.id == "hf:owner/name" and r.source == "huggingface" and r.kind == "dataset"
    assert r.creators == [Creator(name="owner")]
    assert r.year == 2022 and r.license == "mit" and r.access == "open"


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[_DS]))) as c:
        total, recs = await huggingface.search(c, "dna", size=10, offset=5)
    assert (total, recs) == (0, [])


@pytest.mark.asyncio
async def test_resolve_attaches_files_skips_gitattributes():
    body = {**_DS, "siblings": [{"rfilename": ".gitattributes"},
                                {"rfilename": "data/train.parquet"}]}
    async def handler(request):
        assert request.url.path.endswith("/api/datasets/owner/name")
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    names = [f.name for f in r.files]
    assert names == ["data/train.parquet"]
    assert r.files[0].url == "https://huggingface.co/datasets/owner/name/resolve/main/data/train.parquet"


@pytest.mark.asyncio
async def test_resolve_404():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(NotFoundError):
            await huggingface.resolve(c, "hf:owner/missing")


def test_normalize_no_license_tag():
    assert huggingface._normalize({"id": "a/b", "author": "a", "tags": []}).license is None


@pytest.mark.asyncio
async def test_search_gated_is_restricted():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{**_DS, "gated": True}]))) as c:
        _t, recs = await huggingface.search(c, "x", size=5)
    assert recs[0].access == "restricted"
```

- [ ] **Step 2: run → FAIL** (`pytest tests/test_huggingface.py -v`).

- [ ] **Step 3: implement** `huggingface.py` (mirror zenodo's use of `_http.request_json`):

```python
"""HuggingFace Hub *datasets* as a discovery + fetch source."""
from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, compact

API = "https://huggingface.co/api/datasets"
FILE_BASE = "https://huggingface.co/datasets"
PREFIXES = {"hf"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _license(tags: list[str], card: dict | None) -> str | None:
    for t in tags:
        if t.startswith("license:"):
            return t.split(":", 1)[1] or None
    return (card or {}).get("license")


def _normalize(d: dict[str, Any]) -> DataResource:
    ds_id = d.get("id", "")
    tags = d.get("tags") or []
    created = d.get("createdAt") or ""
    year = int(created[:4]) if created[:4].isdigit() else None
    author = d.get("author")
    files = [
        FileEntry(name=s["rfilename"], url=f"{FILE_BASE}/{ds_id}/resolve/main/{s['rfilename']}")
        for s in (d.get("siblings") or [])
        if s.get("rfilename") and s["rfilename"] != ".gitattributes"
    ]
    return DataResource(
        id=f"hf:{ds_id}",
        source="huggingface",
        kind="dataset",
        title=ds_id,
        creators=[Creator(name=author)] if author else [],
        year=year,
        doi=None,
        license=_license(tags, d.get("cardData")),
        subjects=[t for t in tags if ":" not in t],
        access="restricted" if d.get("gated") else "open",
        files=files,
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """Search HF datasets. HF paginates by Link-header cursor, not row offset, so
    this contributes to page 1 only — offset>0 returns no rows (see P4 spec)."""
    if offset:
        return 0, []
    data = await _http.request_json(
        client, "GET", API,
        service="HuggingFace search",
        params={"search": query, "limit": min(size, MAX_SIZE), "full": "true"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT, max_retries=MAX_RETRIES,
    )
    items = data if isinstance(data, list) else []
    return len(items), [compact(_normalize(d)) for d in items]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ds_id = resource_id.split(":", 1)[1] if resource_id.startswith("hf:") else resource_id
    try:
        body = await _http.request_json(
            client, "GET", f"{API}/{ds_id}",
            service="HuggingFace resolve",
            params={"full": "true"},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT, max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"HuggingFace has no dataset {ds_id!r}") from None
    return _normalize(body)
```

VERIFY `_http.request_json`'s real signature + that it raises `NotFoundError` on 404 (check how
zenodo calls it — match exactly; adapt param names if they differ). If `request_json` returns the
parsed JSON (list or dict), the above holds.

- [ ] **Step 4: run → PASS** (`pytest tests/test_huggingface.py -v`).
- [ ] **Step 5: commit** `feat(huggingface): datasets search + resolve adapter`.

---

### Task 2: Router wiring

**Files:** `src/data_aggregator_mcp/router.py`; `tests/test_router.py`.

- [ ] **Step 1: failing tests**

```python
def test_available_sources_includes_huggingface():
    assert "huggingface" in router.available_sources()

async def test_resolve_routes_hf_prefix(monkeypatch):
    called = {}
    async def fake(client, rid):
        called["rid"] = rid
        from data_aggregator_mcp.models import DataResource
        return DataResource(id=rid, source="huggingface", kind="dataset", title="t")
    monkeypatch.setattr(router.huggingface, "resolve", fake)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as c:
        r = await router.resolve(c, "hf:owner/name")
    assert called["rid"] == "hf:owner/name" and r.source == "huggingface"
```

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** — add `from data_aggregator_mcp import huggingface` (match the existing
      import style in router.py), add `"huggingface": huggingface` to `_ADAPTERS`, and add to
      `router.resolve` the branch `elif prefix in huggingface.PREFIXES: resource = await
huggingface.resolve(client, rid)` placed BEFORE the `elif "/" in rid:` (bare-DOI) fallback so an
      `hf:` id never falls through to DataCite. Update the resolve docstring's routing list.
- [ ] **Step 4: run** `pytest tests/test_router.py -v` then full `pytest -q` → PASS (the existing
      `available_sources` assertion that listed exactly 4 sources must be updated to include the 5th).
- [ ] **Step 5: commit** `feat(router): register huggingface source + hf: resolve routing`.

---

### Task 3: Server wiring (fetch gate + list_sources)

**Files:** `src/data_aggregator_mcp/server.py`; `tests/test_server.py`.

- [ ] **Step 1: failing tests**

```python
def test_hf_is_fetchable():
    from data_aggregator_mcp.server import _is_fetchable
    assert _is_fetchable("hf:owner/name") is True

def test_list_sources_includes_huggingface():
    # match how the existing test reaches the list_sources payload
    names = {s["name"] for s in _list_sources_payload()}
    assert "huggingface" in names
```

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** — add `"hf:"` to `_FETCHABLE_SOURCES`; add a `huggingface` dict to
      `_SOURCES` with `filters_supported=["query","size","published_after","published_before","kind","cursor"]`,
      `fetchable=True`, `fetchable_notes="Files downloadable via the HF resolve URL (unverified — no
checksum/size in the API)."`, `id_example="hf:davidcechak/Arabidopsis_thaliana_DNA_v0"`, and a
      one-line `description`. (Match the existing `_SOURCES` entry shape.)
- [ ] **Step 4: run** `pytest tests/test_server.py -v` + full `pytest -q` → PASS.
- [ ] **Step 5: commit** `feat(server): huggingface fetch gate + list_sources entry`.

---

### Task 4: Version bump to 0.15.0 + CHANGELOG

**Files:** `pyproject.toml:3`, `__init__.py:3`, `server.json` ×2, `CHANGELOG.md`, `tests/test_packaging.py`.

- [ ] **Step 1:** update `test_packaging.py` version → `0.15.0` (function name + 3 literals); run → FAIL.
- [ ] **Step 2:** bump the four synced places.
- [ ] **Step 3:** prepend CHANGELOG:

```markdown
## [0.15.0] - 2026-05-31

### Added

- HuggingFace datasets as a search/resolve/fetch source (`hf:<owner>/<name>`). Files
  are fetchable via the HF resolve URL (unverified — the API exposes no checksum/size).
  HF contributes to the first results page only (its API paginates by cursor, not offset).
```

- [ ] **Step 4:** full `pytest -q` → PASS. commit `chore: bump to 0.15.0 (HuggingFace source)`.

---

### Task 5: Real-execution probe

**Files:** `tests/test_huggingface.py` (reuse the `DATA_AGGREGATOR_MCP_LIVE` gate pattern from other test files).

- [ ] **Step 1:** add `@live_only` tests: live `search(client, "dna", size=5)` returns ≥1 record
      whose `id` starts `hf:` and is a well-typed `DataResource`; live `resolve` of a known small
      dataset attaches `files`, and a HEAD/GET of the first file URL returns 2xx/3xx.
- [ ] **Step 2:** run with `DATA_AGGREGATOR_MCP_LIVE=1 ... -k <name>` → PASS against the real API.
- [ ] **Step 3:** commit `test: live HuggingFace search + resolve probe`.

---

## Final review

After all tasks: `pytest -q` green, dispatch a whole-branch code review (focus: resolve routing
order vs the bare-DOI fallback; pagination interaction — HF `(0,[])` on offset>0 must not stall
the cursor; license/tag parsing), then `superpowers:finishing-a-development-branch` to merge and
ship v0.15.0.
