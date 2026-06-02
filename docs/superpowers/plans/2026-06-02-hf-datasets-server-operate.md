# HF datasets-server `operate` backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make any HuggingFace dataset operable (schema/preview/head/sql) by enriching `huggingface.resolve()` with the datasets-server auto-converted Parquet files, surfaced as ordinary `FileEntry`s the existing operate engines already handle.

**Architecture:** New `hf_datasets_server.py` exposes `parquet_files(client, ds_id)`; `huggingface.resolve()` appends them best-effort (404 silent, other errors logged). `operate`, the engines, and `derive_access_modes` are untouched — the capability falls out of the existing file-driven machinery.

**Tech Stack:** Python 3.11+, httpx, pydantic, `_http.request_json`; tests via pytest + `httpx.MockTransport`. No new runtime deps. Run tests with `.venv/bin/python -m pytest` (the project `.venv` carries the `[operate]` extra; conda base does not).

**Spec:** `docs/superpowers/specs/2026-06-02-hf-datasets-server-operate-design.md`

---

## File Structure

- Create: `src/data_aggregator_mcp/hf_datasets_server.py` — datasets-server `/parquet` → `list[FileEntry]`.
- Modify: `src/data_aggregator_mcp/huggingface.py:87-102` — `resolve()` appends the converted Parquet (best-effort).
- Create: `tests/test_hf_datasets_server.py` — unit tests for the module + a LIVE real-execution test.
- Modify: `tests/test_huggingface.py` — enrichment tests + fix the existing resolve test for the new 2nd host call.
- Modify (Task 5): `pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, `server.json`, `CHANGELOG.md`, `README.md`, `tests/test_packaging.py` — bump to `0.20.0`.

**ruff gotcha:** the PostToolUse ruff hook strips imports added before their first use. When adding `import logging` / the `hf_datasets_server` import to `huggingface.py`, add the imports and their usage in the **same** edit (or write the whole file), never imports-first.

---

### Task 1: `hf_datasets_server.parquet_files()`

**Files:**

- Create: `src/data_aggregator_mcp/hf_datasets_server.py`
- Test: `tests/test_hf_datasets_server.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hf_datasets_server.py
import logging

import httpx
import pytest

from data_aggregator_mcp import hf_datasets_server
from data_aggregator_mcp.errors import NotFoundError

_PARQUET_BODY = {
    "parquet_files": [
        {
            "config": "default",
            "split": "test",
            "url": "https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/test/0000.parquet",
            "size": 239511,
        },
        {
            "config": "default",
            "split": "train",
            "url": "https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/train/0000.parquet",
            "size": 1857755,
        },
    ],
    "partial": False,
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_parquet_files_maps_entries():
    async def handler(request):
        assert request.url.host == "datasets-server.huggingface.co"
        assert request.url.path == "/parquet"
        assert request.url.params["dataset"] == "o/n"
        return httpx.Response(200, json=_PARQUET_BODY)

    async with _client(handler) as c:
        files = await hf_datasets_server.parquet_files(c, "o/n")
    assert [f.name for f in files] == [
        "default/test/0000.parquet",
        "default/train/0000.parquet",
    ]
    assert files[0].url == _PARQUET_BODY["parquet_files"][0]["url"]
    assert files[0].size == 239511
    assert all(f.source == "hf-datasets-server" for f in files)


@pytest.mark.asyncio
async def test_parquet_files_empty_list():
    async with _client(lambda r: httpx.Response(200, json={"parquet_files": []})) as c:
        assert await hf_datasets_server.parquet_files(c, "o/n") == []


@pytest.mark.asyncio
async def test_parquet_files_skips_malformed_entries():
    body = {"parquet_files": [{"config": "d", "split": "s"}, {"url": "u"}]}  # all missing a field
    async with _client(lambda r: httpx.Response(200, json=body)) as c:
        assert await hf_datasets_server.parquet_files(c, "o/n") == []


@pytest.mark.asyncio
async def test_parquet_files_caps_and_warns(caplog):
    many = {
        "parquet_files": [
            {"config": "d", "split": "s", "url": f"https://h/x/{i}.parquet", "size": 1}
            for i in range(hf_datasets_server.MAX_DSS_FILES + 5)
        ]
    }
    async with _client(lambda r: httpx.Response(200, json=many)) as c:
        with caplog.at_level(logging.WARNING):
            files = await hf_datasets_server.parquet_files(c, "o/n")
    assert len(files) == hf_datasets_server.MAX_DSS_FILES
    assert any("capping" in m.lower() for m in caplog.messages)


@pytest.mark.asyncio
async def test_parquet_files_404_raises_notfound():
    async with _client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(NotFoundError):
            await hf_datasets_server.parquet_files(c, "o/missing")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_hf_datasets_server.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_aggregator_mcp.hf_datasets_server'`.

- [ ] **Step 3: Write the module**

```python
# src/data_aggregator_mcp/hf_datasets_server.py
"""HuggingFace datasets-server: surface a dataset's auto-converted Parquet files
as FileEntries so the existing operate engines can query any HF dataset."""

from __future__ import annotations

import logging

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

logger = logging.getLogger(__name__)

DSS_API = "https://datasets-server.huggingface.co"
MAX_DSS_FILES = 100
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


async def parquet_files(client: httpx.AsyncClient, ds_id: str) -> list[FileEntry]:
    """The datasets-server auto-converted Parquet files for ``ds_id``.

    Raises ``NotFoundError`` (via ``_http.request_json``) when the dataset has no
    converted view — the caller treats that as the normal "not operable via
    datasets-server" signal and keeps the raw siblings.
    """
    body = await _http.request_json(
        client,
        "GET",
        f"{DSS_API}/parquet",
        service="HF datasets-server",
        params={"dataset": ds_id},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    entries = body.get("parquet_files", []) if isinstance(body, dict) else []
    files = [
        FileEntry(
            name=f"{p['config']}/{p['split']}/{p['url'].rsplit('/', 1)[-1]}",
            url=p["url"],
            size=p.get("size"),
            source="hf-datasets-server",
        )
        for p in entries
        if p.get("url") and p.get("config") and p.get("split")
    ]
    if len(files) > MAX_DSS_FILES:
        logger.warning(
            "datasets-server: %s exposes %d parquet files; capping to %d",
            ds_id,
            len(files),
            MAX_DSS_FILES,
        )
        files = files[:MAX_DSS_FILES]
    return files
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hf_datasets_server.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/hf_datasets_server.py tests/test_hf_datasets_server.py
git commit -m "feat(hf): datasets-server parquet_files -> FileEntry list"
```

---

### Task 2: Enrich `huggingface.resolve()` (best-effort)

**Files:**

- Modify: `src/data_aggregator_mcp/huggingface.py` (imports + `resolve()` at `:87-102`)
- Test: `tests/test_huggingface.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_huggingface.py`)

```python
@pytest.mark.asyncio
async def test_resolve_enriches_with_datasets_server_parquet(monkeypatch):
    from data_aggregator_mcp import hf_datasets_server
    from data_aggregator_mcp.models import FileEntry

    body = {**_DS, "siblings": [{"rfilename": "README.md"}]}

    async def fake_parquet(client, ds_id):
        return [FileEntry(name="default/train/0000.parquet", url="https://h/0.parquet",
                          source="hf-datasets-server")]

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    names = [f.name for f in r.files]
    assert names == ["README.md", "default/train/0000.parquet"]
    assert r.files[-1].source == "hf-datasets-server"


@pytest.mark.asyncio
async def test_resolve_survives_datasets_server_404(monkeypatch):
    from data_aggregator_mcp import hf_datasets_server
    from data_aggregator_mcp.errors import NotFoundError as NFE

    body = {**_DS, "siblings": [{"rfilename": "data/train.parquet"}]}

    async def fake_parquet(client, ds_id):
        raise NFE("no converted view")

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    assert [f.name for f in r.files] == ["data/train.parquet"]  # raw siblings only


@pytest.mark.asyncio
async def test_resolve_logs_on_datasets_server_error(monkeypatch, caplog):
    import logging as _logging

    from data_aggregator_mcp import hf_datasets_server

    body = {**_DS, "siblings": [{"rfilename": "data/train.parquet"}]}

    async def fake_parquet(client, ds_id):
        raise RuntimeError("datasets-server 503")

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        with caplog.at_level(_logging.WARNING):
            r = await huggingface.resolve(c, "hf:owner/name")
    assert [f.name for f in r.files] == ["data/train.parquet"]  # never breaks resolve
    assert any("datasets-server" in m.lower() for m in caplog.messages)
```

Also UPDATE the existing `test_resolve_attaches_files_skips_gitattributes` so its handler answers the new 2nd request (resolve now also calls datasets-server). Replace its `handler` with host-routing that 404s datasets-server (→ enrichment skipped, raw-siblings assertion unchanged):

```python
    async def handler(request):
        if request.url.host == "datasets-server.huggingface.co":
            return httpx.Response(404)
        assert request.url.path.endswith("/api/datasets/owner/name")
        return httpx.Response(200, json=body)
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/python -m pytest tests/test_huggingface.py -q`
Expected: the 3 new tests FAIL (resolve does not yet enrich); `test_resolve_attaches_files_skips_gitattributes` now PASSES with the host-routing handler.

- [ ] **Step 3: Edit `resolve()` — imports + enrichment in ONE edit** (ruff gotcha)

In `huggingface.py`, add at the import block (top, after the existing `from __future__ import annotations` / imports): `import logging`, then `from data_aggregator_mcp import _http, hf_datasets_server` (extend the existing `from data_aggregator_mcp import _http` line), and a module-level `logger = logging.getLogger(__name__)`. Then change the tail of `resolve()` from:

```python
    except NotFoundError:
        raise NotFoundError(f"HuggingFace has no dataset {ds_id!r}") from None
    return _normalize(body)
```

to:

```python
    except NotFoundError:
        raise NotFoundError(f"HuggingFace has no dataset {ds_id!r}") from None
    resource = _normalize(body)
    try:
        resource.files += await hf_datasets_server.parquet_files(client, ds_id)
    except NotFoundError:
        pass  # no converted view — normal (gated / too-big / non-tabular / pending)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break resolve
        logger.warning("datasets-server enrichment failed for %s: %r", ds_id, exc)
    return resource
```

- [ ] **Step 4: Run the HF tests + the FULL suite**

Run: `.venv/bin/python -m pytest tests/test_huggingface.py -q`
Expected: PASS.
Run: `.venv/bin/python -m pytest -q -k "not live"`
Expected: PASS — confirms no _other_ `huggingface.resolve` caller (router/server tests) breaks on the new 2nd host call. If one does, give it the same host-routing/404 handler treatment.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/huggingface.py tests/test_huggingface.py
git commit -m "feat(hf): resolve() appends datasets-server parquet (best-effort)"
```

---

### Task 3: `access_modes` coverage for the converted-Parquet file

**Files:**

- Test: `tests/test_hf_datasets_server.py` (no source change — `derive_access_modes` is already file-driven)

- [ ] **Step 1: Write the test** (append to `tests/test_hf_datasets_server.py`)

```python
def test_converted_parquet_file_advertises_operate_modes():
    from data_aggregator_mcp.models import FileEntry, derive_access_modes

    files = [
        FileEntry(
            name="default/train/0000.parquet",
            url="https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/train/0000.parquet",
            source="hf-datasets-server",
        )
    ]
    assert derive_access_modes(files, operate=True) == [
        "fetch",
        "schema",
        "preview",
        "head",
        "sql",
    ]
```

- [ ] **Step 2: Run it to verify it passes immediately** (proves the design's "zero change" claim)

Run: `.venv/bin/python -m pytest tests/test_hf_datasets_server.py::test_converted_parquet_file_advertises_operate_modes -v`
Expected: PASS with no source change. (If it fails, STOP — the file-driven assumption is wrong and the design needs revisiting.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_hf_datasets_server.py
git commit -m "test(hf): converted parquet advertises operate access_modes"
```

---

### Task 4: LIVE real-execution test (resolve → enrich → operate)

**Files:**

- Test: `tests/test_hf_datasets_server.py` (append; gated by `DATA_AGGREGATOR_MCP_LIVE=1`)

- [ ] **Step 1: Write the live test**

```python
import os

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_enriches_and_operates():
    from data_aggregator_mcp import huggingface, operate

    ds = "hf:mteb/tweet_sentiment_extraction"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await huggingface.resolve(c, ds)
        dss = [f for f in r.files if f.source == "hf-datasets-server"]
        assert dss, "resolve should surface datasets-server parquet files"

        sch = await operate.run(c, ds, "schema", file=dss[0].name)
        assert sch["columns"]
        rows = await operate.run(
            c, ds, "sql", query="SELECT * FROM data LIMIT 5", file=dss[0].name
        )
        assert len(rows["rows"]) == 5
```

- [ ] **Step 2: Run base-only (skips) then live (executes)**

Run: `.venv/bin/python -m pytest tests/test_hf_datasets_server.py -q`
Expected: the live test is SKIPPED; all unit tests PASS.
Run: `DATA_AGGREGATOR_MCP_LIVE=1 .venv/bin/python -m pytest tests/test_hf_datasets_server.py::test_live_resolve_enriches_and_operates -v`
Expected: PASS against the live datasets-server + HF CDN (proves the whole chain).

- [ ] **Step 3: Commit**

```bash
git add tests/test_hf_datasets_server.py
git commit -m "test(hf): live resolve-enrich-operate real-execution check"
```

---

### Task 5: Version bump + docs → 0.20.0

**Files:**

- Modify: `pyproject.toml`, `src/data_aggregator_mcp/__init__.py:3`, `server.json:10` + `:16`, `CHANGELOG.md`, `README.md`, `tests/test_packaging.py:14-16,45`

- [ ] **Step 1: Update the packaging test first (TDD on the bump)**

In `tests/test_packaging.py`: rename `test_version_is_0190_and_synced` → `test_version_is_0200_and_synced`; change the two `"0.19.0"` asserts (lines 15-16) and the `sj["version"] == "0.19.0"` assert (line 45) to `"0.20.0"`.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -q`
Expected: FAIL — version sites still say `0.19.0`.

- [ ] **Step 3: Bump the three version sites**

- `src/data_aggregator_mcp/__init__.py:3` → `__version__ = "0.20.0"`
- `pyproject.toml` → `version = "0.20.0"`
- `server.json` → top-level `"version": "0.20.0"` (line 10) AND `packages[0].version` `"0.20.0"` (line 16)

- [ ] **Step 4: Run packaging test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -q`
Expected: PASS.

- [ ] **Step 5: CHANGELOG + README**

Insert above the `## [0.19.0]` section in `CHANGELOG.md`:

```markdown
## [0.20.0] - 2026-06-02

### Added

- HuggingFace datasets are now operable via the datasets-server auto-converted
  Parquet: `huggingface.resolve()` surfaces those files (`source="hf-datasets-server"`),
  so `operate` (schema/preview/head/sql) reaches datasets stored as JSON/JSONL/arrow,
  not only ones that ship `.parquet` at the raw URL. Best-effort: a dataset with no
  converted view (gated/too-big/pending) keeps its raw siblings unchanged.
```

In `README.md`, under the operate / HuggingFace section, add a sentence: any HuggingFace dataset with a datasets-server converted view is operable (schema/preview/head/sql) — `resolve` surfaces the converted Parquet files; pass `file=<config>/<split>/...parquet` to pick a split.

- [ ] **Step 6: Full suite + commit**

Run: `.venv/bin/python -m pytest -q -k "not live"`
Expected: PASS.

```bash
git add pyproject.toml src/data_aggregator_mcp/__init__.py server.json CHANGELOG.md README.md tests/test_packaging.py
git commit -m "chore: bump to 0.20.0 (HF datasets-server operate backend)"
```

---

## Self-Review

- **Spec coverage:** module (`parquet_files`) → Task 1; resolve enrichment + fail-soft policy → Task 2; `access_modes` honesty → Task 3; live boundary check → Task 4; release → Task 5. All spec sections covered.
- **Integration risk:** the enrichment adds a 2nd host call to every `huggingface.resolve` — Task 2 fixes the existing resolve test and runs the full suite to catch any other resolve caller (router/server tests).
- **Type/name consistency:** `parquet_files(client, ds_id) -> list[FileEntry]`, `MAX_DSS_FILES`, `source="hf-datasets-server"` used identically across module, tests, and resolve.
- **Out of scope honored:** no `/first-rows`/`/rows`, no auth, no operate/engine changes.
