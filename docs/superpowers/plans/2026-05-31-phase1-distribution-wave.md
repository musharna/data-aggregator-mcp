# Phase 1 + Distribution Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the cheap, high-leverage "complete MCP citizen + confident-pick" layer (structured-output safety gate, trust metrics, version/freshness signals, tool annotations, prompts, Croissant + RO-Crate export) and, in parallel, the distribution actions that turn a published-but-invisible server into a discoverable one.

**Architecture:** Every code change is additive to the existing normalized `DataResource` contract and the `search → dedup → enrich → resolve → fetch` pipeline (`router.py`, `server.py`). New optional model fields (`metrics`, `is_latest`, `superseded_by`, `last_updated`, `croissant`, `ro_crate`) are nullable so they never break a deserializer. A schema round-trip gate (Task 1) lands FIRST because the MCP SDK (1.27+) validates each tool's returned dict against its declared `outputSchema` on every call — additive fields are contract changes, not free. Export renderers (`croissant.py`, `ro_crate.py`) are pure transforms mirroring the existing `citation.render` pattern. Distribution is a parallel non-code track.

**Tech Stack:** Python 3.11+, `httpx` (async), `pydantic` v2, `mcp` SDK (stdio), `jsonschema` (new dev dep, for the gate), `pytest`/`pytest-asyncio`/`pytest-httpx`.

**Source of record:** master plan `docs/superpowers/plans/2026-05-31-one-stop-shop-master-plan.md` (§"Resolved sequencing") + gap-analysis memo `~/.claude/projects/-home-mjarnold/memory/data_aggregator_master_plan_gap_analysis_2026-05-31.md`.

**Explicitly OUT of this wave (YAGNI / deferred):**

- P1.9 relationType-canonicalization + ROR extraction — `scholix.py` already maps Scholix rels and `models._rel` snake-cases DataCite rels; the residual (ROR id extraction, verbatim relationType vocab) is low-value and gets its own minor tier later.
- `last_updated` for `zenodo`/`omics`/`literature` adapters — this wave populates it only for the two adapters already verified (`datacite`, `huggingface`); the others are a typed follow-up that must read each adapter's normalizer first.
- Full Croissant `RecordSet`/`Field` manifest — needs per-column structure from the P2.4 footer-introspection capability (a later phase). This wave ships the **file-level subset** only, documented as such.

---

## Current-state ground truth (verified live this session — do not re-derive from memory)

- `models.py`: `DataResource` (lines 58-77) has fields `id, source, kind, title, creators, funding, year, description, doi, identifiers, accessions, organism, taxa, subjects, license, access, files, links, citation`. It has **no** `metrics`/`is_latest`/`superseded_by`/`last_updated`/`croissant`/`ro_crate`. Imports are `re` + `pydantic` only (no `typing.Any` yet). `compact()` (lines 109-115) drops `files` + truncates `description`, **keeps `links`**.
- `server.py`: `TOOLS` (line 179) defines 4 tools, each already with `outputSchema=...model_json_schema()`. **No** `annotations`, **no** prompts. `resolve` tool inputSchema has `id` + `cite` only. `_dispatch` (line 357) returns `model_dump()`; the `resolve` case (line 380) renders `citation` via `citation.render`. `_call_tool` (line 436) returns the bare dict.
- `router.py`: `_VALID_KINDS` (line 35) = `{"dataset","sequencing_run","study","publication","software"}`. `resolve()` (line 368) routes by prefix, enriches taxa/links, caches. `search_page()` builds `SearchResult`.
- `datacite.py` `_normalize` (line 129) reads `item["attributes"]` but pulls **no** metrics. `huggingface.py` `_normalize` (line 29) reads the dataset dict but pulls **no** metrics.
- `citation.py` `render(client, resource, fmt)` (line 56) is the fail-soft enrichment pattern to mirror.
- `scholix.py` already exists and maps Scholix relationship names → our rel vocab.
- `tests/test_server.py`: `test_all_tool_outputs_validate_against_schemas` (lines 102-108) only calls `.model_dump()` — it does NOT validate against the schema. `test_search_schema_exposes_pagination_and_filters` (lines 219-229) pins the `kind` enum verbatim.
- `pyproject.toml`: deps = `mcp>=1.0, httpx>=0.27, pydantic>=2.6`; dev extras at lines 34-42 (no `jsonschema`). Version `0.16.0` (line 3). `src/.../__init__.py` `__version__ = "0.16.0"`. `server.json` exists.
- SDK verified: `mcp.types.ToolAnnotations(readOnlyHint=...)`, `types.Tool(annotations=...)`, `types.Prompt(name, description, arguments=[PromptArgument(name, description, required)])`, `types.PromptMessage(role, content=TextContent(type="text", text=...))`, `types.GetPromptResult(messages=[...], description=...)`, `@server.list_prompts()`/`@server.get_prompt()`. `jsonschema` 4.26 importable.

---

## File Structure

| File                                     | Responsibility                                 | Change                                                            |
| ---------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------- |
| `pyproject.toml`                         | deps                                           | add `jsonschema` to dev extras; bump version                      |
| `tests/test_output_schema_gate.py`       | the standing structured-output round-trip gate | **create**                                                        |
| `src/data_aggregator_mcp/models.py`      | the wire contract                              | add `Metrics` model + 6 nullable fields + `derive_version_status` |
| `src/data_aggregator_mcp/datacite.py`    | DataCite normalize                             | populate `metrics` + `last_updated`                               |
| `src/data_aggregator_mcp/huggingface.py` | HF normalize                                   | populate `metrics` + `last_updated`                               |
| `src/data_aggregator_mcp/router.py`      | resolve/search wiring                          | apply `derive_version_status`                                     |
| `src/data_aggregator_mcp/server.py`      | MCP surface                                    | annotations, prompts, `resolve(format=)`                          |
| `src/data_aggregator_mcp/croissant.py`   | Croissant file-level export                    | **create**                                                        |
| `src/data_aggregator_mcp/ro_crate.py`    | RO-Crate export                                | **create**                                                        |
| `CHANGELOG.md`                           | milestone log                                  | append 0.17.0                                                     |
| `glama.json`                             | directory listing manifest                     | **create** (distribution)                                         |

---

### Task 1: Structured-output round-trip gate (PREREQUISITE — blocks every later field)

**Why first:** the MCP SDK validates the dict a tool returns against its declared `outputSchema`. Any drift between `model_dump()` and `model_json_schema()` returns an error result to the client. This gate makes that drift a failing test instead of a runtime surprise, so every later field-adding task re-runs it.

**Files:**

- Modify: `pyproject.toml:34-42` (dev extras)
- Create: `tests/test_output_schema_gate.py`

- [ ] **Step 1: Add `jsonschema` to dev extras**

In `pyproject.toml`, the `dev` list (currently lines 35-42) gains one entry:

```toml
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-httpx>=0.30",
    "pytest-cov>=5.0",
    "ruff>=0.3",
    "pyyaml>=6.0",
    "jsonschema>=4.20",
]
```

- [ ] **Step 2: Write the gate test**

Create `tests/test_output_schema_gate.py`:

```python
"""Standing gate: each tool's representative output must validate against the
outputSchema the tool declares. The MCP SDK runs this same validation at
runtime, so a mismatch here is a real client-facing break, not a test nicety.

When a later task adds a DataResource/SearchResult field, populate it in the
representative instance below so the gate exercises the new field too.
"""

from __future__ import annotations

import jsonschema

from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FetchResult,
    FileEntry,
    Link,
    SearchResult,
)


def _validate(instance_model, schema_model) -> None:
    """Dump the model the way MCP serializes it (JSON mode) and validate it
    against the model's own JSON schema."""
    payload = instance_model.model_dump(mode="json")
    jsonschema.validate(payload, schema_model.model_json_schema())


def _sample_resource() -> DataResource:
    return DataResource(
        id="datacite:10.5061/dryad.x",
        source="dryad",
        kind="dataset",
        title="t",
        creators=[Creator(name="A. Author", orcid="0000-0002-1825-0097")],
        year=2024,
        description="d",
        doi="10.5061/dryad.x",
        files=[FileEntry(name="a.csv", url="https://x/a.csv", checksum="md5:abc")],
        links=[Link(rel="is_supplement_to", target_id="datacite:10.1/y")],
    )


def test_dataresource_dump_validates_against_schema() -> None:
    _validate(_sample_resource(), DataResource)


def test_searchresult_dump_validates_against_schema() -> None:
    sr = SearchResult(query="q", total=1, count=1, results=[_sample_resource()])
    _validate(sr, SearchResult)


def test_fetchresult_dump_validates_against_schema() -> None:
    _validate(FetchResult(paths=["/tmp/a"], bytes=1), FetchResult)
```

- [ ] **Step 3: Run the gate**

Run: `pip install -e ".[dev]" && pytest tests/test_output_schema_gate.py -v`
Expected: 3 PASS (proves current models conform — the gate is now armed).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/test_output_schema_gate.py
git commit -m "test: add structured-output round-trip gate (jsonschema dev dep)"
```

---

### Task 2: `Metrics` model + nullable `metrics` field

**Files:**

- Modify: `src/data_aggregator_mcp/models.py` (add `Metrics` near other models; add field to `DataResource`)
- Modify: `tests/test_output_schema_gate.py` (exercise populated metrics)
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_metrics_default_none_and_populated_roundtrip() -> None:
    from data_aggregator_mcp.models import DataResource, Metrics

    bare = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert bare.metrics is None

    m = DataResource(
        id="datacite:10.x/y",
        source="dryad",
        kind="dataset",
        title="t",
        metrics=Metrics(citations=3, views=100, downloads=42, likes=None),
    )
    dumped = m.model_dump()
    assert dumped["metrics"]["citations"] == 3
    assert dumped["metrics"]["likes"] is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_models.py::test_metrics_default_none_and_populated_roundtrip -v`
Expected: FAIL with `ImportError: cannot import name 'Metrics'`.

- [ ] **Step 3: Add the model + field**

In `src/data_aggregator_mcp/models.py`, add the `Metrics` class above `class DataResource` (after `Taxon`, around line 56):

```python
class Metrics(BaseModel):
    """Usage/impact signals, each a separate axis — NO blended score. All
    nullable: a source that does not expose an axis leaves it None."""

    citations: int | None = None
    views: int | None = None
    downloads: int | None = None
    likes: int | None = None
```

Then add the field to `DataResource` (after the `citation` field, line 77):

```python
    metrics: Metrics | None = None  # usage/impact signals, source-dependent
```

- [ ] **Step 4: Run the test + the gate**

Run: `pytest tests/test_models.py::test_metrics_default_none_and_populated_roundtrip tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Extend the gate to cover populated metrics**

In `tests/test_output_schema_gate.py`, edit `_sample_resource()` to add `metrics`:

```python
        metrics=Metrics(citations=3, views=100, downloads=42),
```

and add `Metrics` to the import line from `data_aggregator_mcp.models`. Re-run the gate:

Run: `pytest tests/test_output_schema_gate.py -v`
Expected: PASS (a nested model still validates).

- [ ] **Step 6: Commit**

```bash
git add src/data_aggregator_mcp/models.py tests/test_models.py tests/test_output_schema_gate.py
git commit -m "feat(models): add Metrics (separate axes, no blended score) + nullable metrics field"
```

---

### Task 3: Populate `metrics` from DataCite

DataCite `/dois` records carry `attributes.citationCount`, `attributes.viewCount`, `attributes.downloadCount` (confirmed live 2026-05-31; sparse for non-participating repos).

**Files:**

- Modify: `src/data_aggregator_mcp/datacite.py` (`_normalize`, line 129)
- Test: `tests/test_datacite.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_datacite.py`:

```python
def test_normalize_populates_metrics_from_attributes() -> None:
    from data_aggregator_mcp.datacite import _normalize

    item = {
        "attributes": {
            "doi": "10.5061/dryad.x",
            "titles": [{"title": "t"}],
            "types": {"resourceTypeGeneral": "Dataset"},
            "citationCount": 5,
            "viewCount": 200,
            "downloadCount": 17,
        }
    }
    r = _normalize(item)
    assert r.metrics is not None
    assert r.metrics.citations == 5
    assert r.metrics.views == 200
    assert r.metrics.downloads == 17


def test_normalize_metrics_none_when_absent() -> None:
    from data_aggregator_mcp.datacite import _normalize

    item = {"attributes": {"doi": "10.5061/dryad.y", "titles": [{"title": "t"}]}}
    assert _normalize(item).metrics is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_datacite.py::test_normalize_populates_metrics_from_attributes -v`
Expected: FAIL (`r.metrics` is None).

- [ ] **Step 3: Implement**

In `datacite.py`, import `Metrics`:

```python
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FundingRef,
    Link,
    Metrics,
    _orcid,
    _rel,
    compact,
)
```

Add a helper above `_normalize` (after `_access_from_rights`, ~line 111):

```python
def _metrics(a: dict[str, Any]) -> Metrics | None:
    """Pull DataCite's inline usage counts. Returns None when none are present
    so the field stays absent rather than a zero-filled object."""
    cites, views, dls = a.get("citationCount"), a.get("viewCount"), a.get("downloadCount")
    if cites is None and views is None and dls is None:
        return None
    return Metrics(citations=cites, views=views, downloads=dls)
```

In `_normalize`, add `metrics=_metrics(a),` to the `DataResource(...)` constructor (e.g. right after `access=_access_from_rights(rights),`, line 156).

- [ ] **Step 4: Run the tests + gate**

Run: `pytest tests/test_datacite.py tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/datacite.py tests/test_datacite.py
git commit -m "feat(datacite): populate Metrics from inline citation/view/download counts"
```

---

### Task 4: Populate `metrics` from HuggingFace

HF `/api/datasets` records (with `full=true`) carry top-level `downloads` and `likes`.

**Files:**

- Modify: `src/data_aggregator_mcp/huggingface.py` (`_normalize`, line 29)
- Test: `tests/test_huggingface.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_huggingface.py`:

```python
def test_normalize_populates_metrics_from_hf_fields() -> None:
    from data_aggregator_mcp.huggingface import _normalize

    d = {"id": "owner/name", "downloads": 1234, "likes": 9, "createdAt": "2024-01-01"}
    r = _normalize(d)
    assert r.metrics is not None
    assert r.metrics.downloads == 1234
    assert r.metrics.likes == 9
    assert r.metrics.citations is None


def test_normalize_metrics_none_when_hf_counts_absent() -> None:
    from data_aggregator_mcp.huggingface import _normalize

    assert _normalize({"id": "owner/name"}).metrics is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_huggingface.py::test_normalize_populates_metrics_from_hf_fields -v`
Expected: FAIL (`r.metrics` is None).

- [ ] **Step 3: Implement**

In `huggingface.py`, change the import line (line 11):

```python
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Metrics, compact
```

Add a helper above `_normalize` (after `_license`, ~line 27):

```python
def _metrics(d: dict[str, Any]) -> Metrics | None:
    dls, likes = d.get("downloads"), d.get("likes")
    if dls is None and likes is None:
        return None
    return Metrics(downloads=dls, likes=likes)
```

In `_normalize`, add `metrics=_metrics(d),` to the `DataResource(...)` constructor (e.g. after `access=...`, line 51).

- [ ] **Step 4: Run the tests + gate**

Run: `pytest tests/test_huggingface.py tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/huggingface.py tests/test_huggingface.py
git commit -m "feat(huggingface): populate Metrics from downloads/likes"
```

---

### Task 5: `is_latest` + `superseded_by` (fields only — NO ranking change)

Derive version status from the `links[]` we already parse. Per the resolved sequencing, this adds **fields only**; default search ordering is unchanged.

**Files:**

- Modify: `src/data_aggregator_mcp/models.py` (2 fields + `derive_version_status`)
- Modify: `src/data_aggregator_mcp/router.py` (apply in `resolve` + on `search_page` emitted results)
- Test: `tests/test_models.py`, `tests/test_router.py`

- [ ] **Step 1: Write the failing test for the pure function**

Append to `tests/test_models.py`:

```python
def test_derive_version_status() -> None:
    from data_aggregator_mcp.models import Link, derive_version_status

    # superseded: a newer version exists
    older = [Link(rel="is_previous_version_of", target_id="datacite:10.x/v2")]
    assert derive_version_status(older) == (False, "datacite:10.x/v2")

    # obsoleted alias
    obs = [Link(rel="is_obsoleted_by", target_id="datacite:10.x/v3")]
    assert derive_version_status(obs) == (False, "datacite:10.x/v3")

    # latest: it supersedes an older one but nothing supersedes it
    newest = [Link(rel="is_new_version_of", target_id="datacite:10.x/v1")]
    assert derive_version_status(newest) == (True, None)

    # no version info at all
    assert derive_version_status([Link(rel="is_supplement_to", target_id="x")]) == (None, None)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_models.py::test_derive_version_status -v`
Expected: FAIL (`cannot import name 'derive_version_status'`).

- [ ] **Step 3: Add the fields + function**

In `models.py`, add to `DataResource` (after the `metrics` field from Task 2):

```python
    is_latest: bool | None = None  # None = no version info in links[]
    superseded_by: str | None = None  # id of the newer version, when known
```

Add the pure function at the end of `models.py`:

```python
# links[].rel values that say "a NEWER version of me exists" (I am superseded).
_SUPERSEDED_BY_RELS = {"is_previous_version_of", "is_obsoleted_by"}
# links[].rel values that say "I supersede / version an OLDER record".
_SUPERSEDES_RELS = {"is_new_version_of", "obsoletes", "has_version", "is_version_of"}


def derive_version_status(links: list["Link"]) -> tuple[bool | None, str | None]:
    """Infer (is_latest, superseded_by) from version relations in links[].
    Returns (None, None) when links carry no version information at all —
    absence of evidence, not a claim of latest."""
    for lnk in links:
        if lnk.rel in _SUPERSEDED_BY_RELS:
            return False, lnk.target_id
    if any(lnk.rel in _SUPERSEDES_RELS for lnk in links):
        return True, None
    return None, None
```

- [ ] **Step 4: Run the function test**

Run: `pytest tests/test_models.py::test_derive_version_status -v`
Expected: PASS.

- [ ] **Step 5: Write the failing wiring test**

Append to `tests/test_router.py`:

```python
async def test_resolve_sets_version_status(monkeypatch) -> None:
    import httpx

    from data_aggregator_mcp import router
    from data_aggregator_mcp.models import DataResource, Link

    async def fake_zenodo_resolve(client, rid):
        return DataResource(
            id="zenodo:1",
            source="zenodo",
            kind="dataset",
            title="t",
            links=[Link(rel="is_previous_version_of", target_id="zenodo:2")],
        )

    monkeypatch.setattr("data_aggregator_mcp.zenodo.resolve", fake_zenodo_resolve)
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "zenodo:1")
    assert r.is_latest is False
    assert r.superseded_by == "zenodo:2"
```

Confirm `_RESOLVE_CACHE` exposes `.clear()`; if not, use a fresh id per test run instead. (`TTLCache` in `_cache.py` — check its API at implementation time; if no `clear`, resolve a unique id like `"zenodo:victest"`.)

- [ ] **Step 6: Run it to verify it fails**

Run: `pytest tests/test_router.py::test_resolve_sets_version_status -v`
Expected: FAIL (`is_latest` is None).

- [ ] **Step 7: Wire into `router.resolve`**

In `router.py`, import the helper (extend the existing `models` import, line 33):

```python
from data_aggregator_mcp.models import (
    DataResource,
    Link,
    SearchResult,
    Taxon,
    TaxonExpansion,
    derive_version_status,
)
```

In `resolve()`, after the enrichment block and before `_RESOLVE_CACHE.set(rid, resource)` (line 404), add:

```python
    is_latest, superseded_by = derive_version_status(resource.links)
    if is_latest is not None or superseded_by is not None:
        resource = resource.model_copy(
            update={"is_latest": is_latest, "superseded_by": superseded_by}
        )
```

- [ ] **Step 8: Wire into `search_page` emitted results**

In `router.py` `search_page`, the emitted results are enriched at line 340 (`enriched = await _enrich(client, emitted, errors)`). Add a mapping pass immediately after that line:

```python
    enriched = [_with_version_status(r) for r in enriched]
```

and define a module-level helper near `_passes_filters`:

```python
def _with_version_status(r: DataResource) -> DataResource:
    is_latest, superseded_by = derive_version_status(r.links)
    if is_latest is None and superseded_by is None:
        return r
    return r.model_copy(update={"is_latest": is_latest, "superseded_by": superseded_by})
```

- [ ] **Step 9: Run the wiring test + gate + full router suite**

Run: `pytest tests/test_router.py tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/data_aggregator_mcp/models.py src/data_aggregator_mcp/router.py tests/test_models.py tests/test_router.py
git commit -m "feat: derive is_latest/superseded_by from version links (fields only, no ranking change)"
```

---

### Task 6: `last_updated` freshness (datacite + huggingface only)

Adds the field and populates it for the two adapters already verified. Other adapters are an explicit follow-up (see OUT-of-scope).

**Files:**

- Modify: `src/data_aggregator_mcp/models.py` (1 field)
- Modify: `src/data_aggregator_mcp/datacite.py`, `src/data_aggregator_mcp/huggingface.py`
- Test: `tests/test_datacite.py`, `tests/test_huggingface.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_datacite.py`:

```python
def test_normalize_populates_last_updated() -> None:
    from data_aggregator_mcp.datacite import _normalize

    item = {"attributes": {"doi": "10.x/y", "titles": [{"title": "t"}], "updated": "2025-03-04T00:00:00Z"}}
    assert _normalize(item).last_updated == "2025-03-04T00:00:00Z"
```

Append to `tests/test_huggingface.py`:

```python
def test_normalize_populates_last_updated() -> None:
    from data_aggregator_mcp.huggingface import _normalize

    d = {"id": "owner/name", "lastModified": "2025-06-01T12:00:00.000Z"}
    assert _normalize(d).last_updated == "2025-06-01T12:00:00.000Z"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_datacite.py::test_normalize_populates_last_updated tests/test_huggingface.py::test_normalize_populates_last_updated -v`
Expected: FAIL (`last_updated` unknown / None).

- [ ] **Step 3: Add the field**

In `models.py` `DataResource`, after `superseded_by`:

```python
    last_updated: str | None = None  # source's modified/updated timestamp (ISO 8601)
```

- [ ] **Step 4: Populate in both adapters**

In `datacite.py` `_normalize`, add to the constructor: `last_updated=a.get("updated"),`.
In `huggingface.py` `_normalize`, add to the constructor: `last_updated=d.get("lastModified"),`.

- [ ] **Step 5: Run the tests + gate**

Run: `pytest tests/test_datacite.py tests/test_huggingface.py tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/data_aggregator_mcp/models.py src/data_aggregator_mcp/datacite.py src/data_aggregator_mcp/huggingface.py tests/test_datacite.py tests/test_huggingface.py
git commit -m "feat: add last_updated freshness (datacite + huggingface)"
```

---

### Task 7: Tool annotations (`readOnlyHint`)

`search`/`resolve`/`list_sources` are read-only; `fetch` writes to a caller-named dest (not destructive to existing state, not idempotent). Annotations smooth client auto-approval.

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (each `types.Tool(...)` in `TOOLS`, line 179)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py`:

```python
def test_read_only_tools_are_annotated() -> None:
    from data_aggregator_mcp import server

    by_name = {t.name: t for t in server.TOOLS}
    for n in ("search", "resolve", "list_sources"):
        assert by_name[n].annotations is not None
        assert by_name[n].annotations.readOnlyHint is True
    # fetch writes files → not read-only, and not destructive to existing state
    assert by_name["fetch"].annotations.readOnlyHint is False
    assert by_name["fetch"].annotations.destructiveHint is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_server.py::test_read_only_tools_are_annotated -v`
Expected: FAIL (`annotations is None`).

- [ ] **Step 3: Add annotations**

In `server.py`, add `annotations=types.ToolAnnotations(readOnlyHint=True)` to the `search`, `resolve`, and `list_sources` `types.Tool(...)` definitions (alongside their existing `outputSchema=`). For `fetch`:

```python
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_server.py::test_read_only_tools_are_annotated -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): add readOnlyHint/destructiveHint tool annotations"
```

---

### Task 8: Server prompts

Register `list_prompts`/`get_prompt` with three workflow templates. Surfaces in clients as `/mcp__data_aggregator__*`.

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (add handlers after `_list_tools`, line 354)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py`:

```python
async def test_list_prompts_exposes_templates() -> None:
    from data_aggregator_mcp import server

    prompts = await server._list_prompts()
    names = {p.name for p in prompts}
    assert {"find_data", "data_behind_paper", "search_resolve_fetch"} <= names


async def test_get_prompt_find_data_includes_topic() -> None:
    from data_aggregator_mcp import server

    result = await server._get_prompt("find_data", {"topic": "rice drought", "organism": "Oryza sativa"})
    text = result.messages[0].content.text
    assert "rice drought" in text
    assert "Oryza sativa" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_server.py::test_list_prompts_exposes_templates -v`
Expected: FAIL (`module has no attribute '_list_prompts'`).

- [ ] **Step 3: Implement the handlers**

In `server.py`, after `_list_tools` (line 354), add:

```python
_PROMPTS: list[types.Prompt] = [
    types.Prompt(
        name="find_data",
        description="Find datasets/data for a topic, optionally scoped to an organism.",
        arguments=[
            types.PromptArgument(name="topic", description="What to find data about", required=True),
            types.PromptArgument(
                name="organism", description="Optional organism to expand via NCBI Taxonomy", required=False
            ),
        ],
    ),
    types.Prompt(
        name="data_behind_paper",
        description="Find the datasets / accessions behind a paper (by DOI, PMID, or title).",
        arguments=[
            types.PromptArgument(name="paper", description="DOI, 'pubmed:<id>', or paper title", required=True),
        ],
    ),
    types.Prompt(
        name="search_resolve_fetch",
        description="Walk the search → resolve → fetch flow for a data need.",
        arguments=[
            types.PromptArgument(name="need", description="What data is needed", required=True),
        ],
    ),
]


@server.list_prompts()
async def _list_prompts() -> list[types.Prompt]:
    return _PROMPTS


def _prompt_text(name: str, args: dict[str, str]) -> str:
    if name == "find_data":
        topic = args.get("topic", "")
        organism = args.get("organism")
        org = f" Pass organism='{organism}' to expand the query with NCBI-Taxonomy synonyms." if organism else ""
        return (
            f"Use the data-aggregator `search` tool to find datasets about: {topic}.{org} "
            "Review the compact results, then `resolve` the most relevant id for its full "
            "files[] manifest, and `fetch` to download."
        )
    if name == "data_behind_paper":
        paper = args.get("paper", "")
        return (
            f"Find the data behind '{paper}'. If it is a DOI/PMID, `resolve` it — publication "
            "resolve attaches links[] to datasets/accessions and normalized identifiers. Then "
            "`resolve`/`fetch` each linked dataset. Otherwise `search` for the paper first."
        )
    if name == "search_resolve_fetch":
        need = args.get("need", "")
        return (
            f"Goal: {need}. 1) `search` (add organism= to expand taxonomy synonyms). "
            "2) `resolve` a chosen id for the full record + files[]. 3) `fetch` to download. "
            "Use `list_sources` to see which sources are fetchable."
        )
    raise ValueError(f"unknown prompt: {name}")


@server.get_prompt()
async def _get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = arguments or {}
    text = _prompt_text(name, args)
    return types.GetPromptResult(
        description=next((p.description for p in _PROMPTS if p.name == name), None),
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=text)),
        ],
    )
```

- [ ] **Step 4: Run the tests + entrypoint smoke**

Run: `pytest tests/test_server.py tests/test_entrypoint_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): register prompts (find_data, data_behind_paper, search_resolve_fetch)"
```

---

### Task 9: Croissant file-level export

A pure transform mirroring `citation.render`. Adds a nullable `croissant` field and a `format` arg on `resolve`. **File-level subset only** — no `RecordSet`/`Field` (that needs the later P2.4 footer capability); documented as such in the module docstring.

**Files:**

- Create: `src/data_aggregator_mcp/croissant.py`
- Modify: `src/data_aggregator_mcp/models.py` (1 field + `Any` import)
- Modify: `src/data_aggregator_mcp/server.py` (`resolve` tool `format` arg + `_dispatch` resolve case)
- Test: `tests/test_croissant.py` (create), `tests/test_server.py`

- [ ] **Step 1: Write the failing renderer test**

Create `tests/test_croissant.py`:

```python
from data_aggregator_mcp import croissant
from data_aggregator_mcp.models import Creator, DataResource, FileEntry


def _resource() -> DataResource:
    return DataResource(
        id="datacite:10.5061/dryad.x",
        source="dryad",
        kind="dataset",
        title="Rice genomes",
        description="d",
        doi="10.5061/dryad.x",
        creators=[Creator(name="A. Author")],
        year=2024,
        license="cc-by-4.0",
        files=[
            FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=10, checksum="sha256:deadbeef"),
            FileEntry(name="b.bin", url="https://x/b.bin", checksum="md5:abc123"),
        ],
    )


def test_render_produces_file_level_croissant() -> None:
    m = croissant.render(_resource())
    assert m["@type"] == "Dataset"
    assert m["name"] == "Rice genomes"
    assert m["license"] == "cc-by-4.0"
    dist = {f["name"]: f for f in m["distribution"]}
    assert dist["a.csv"]["@type"] == "cr:FileObject"
    assert dist["a.csv"]["contentUrl"] == "https://x/a.csv"
    assert dist["a.csv"]["encodingFormat"] == "text/csv"
    assert dist["a.csv"]["sha256"] == "deadbeef"
    assert dist["b.bin"]["md5"] == "abc123"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_croissant.py -v`
Expected: FAIL (`No module named 'data_aggregator_mcp.croissant'`).

- [ ] **Step 3: Implement the renderer**

Create `src/data_aggregator_mcp/croissant.py`:

```python
"""Croissant export — file-level subset of the Croissant 1.1 metadata format.

This renders the dataset + its FileObjects (the file-level layer). It does NOT
emit RecordSet/Field structures: those describe tabular column semantics, which
require reading file internals (a later operate-on-data capability). The output
is therefore a valid schema.org Dataset with Croissant FileObject distributions,
not a RecordSet-complete 1.1 manifest. Pure transform — never does I/O.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp.models import DataResource

_CONTEXT = {
    "@vocab": "https://schema.org/",
    "cr": "http://mlcommons.org/croissant/",
    "sc": "https://schema.org/",
}


def _file_object(name: str, url: str | None, mime: str | None, size: int | None, checksum: str | None) -> dict[str, Any]:
    obj: dict[str, Any] = {"@type": "cr:FileObject", "@id": name, "name": name}
    if url:
        obj["contentUrl"] = url
    if mime:
        obj["encodingFormat"] = mime
    if size is not None:
        obj["contentSize"] = size
    if checksum and ":" in checksum:
        algo, _, hexval = checksum.partition(":")
        if algo in ("sha256", "md5"):
            obj[algo] = hexval
    return obj


def render(r: DataResource) -> dict[str, Any]:
    """Render ``r`` as a file-level Croissant JSON-LD object."""
    out: dict[str, Any] = {
        "@context": _CONTEXT,
        "@type": "Dataset",
        "name": r.title,
    }
    if r.description:
        out["description"] = r.description
    if r.doi:
        out["identifier"] = f"https://doi.org/{r.doi}"
    if r.license:
        out["license"] = r.license
    if r.year:
        out["datePublished"] = str(r.year)
    if r.creators:
        out["creator"] = [{"@type": "Person", "name": c.name} for c in r.creators]
    out["distribution"] = [
        _file_object(f.name, f.url, f.mime, f.size, f.checksum) for f in r.files
    ]
    return out
```

- [ ] **Step 4: Run the renderer test**

Run: `pytest tests/test_croissant.py -v`
Expected: PASS.

- [ ] **Step 5: Add the field + wire `resolve(format=)`**

In `models.py`, add at the top (after `import re`):

```python
from typing import Any
```

Add to `DataResource` (after `last_updated`):

```python
    croissant: dict[str, Any] | None = None  # file-level Croissant export, on resolve(format=croissant)
```

In `server.py`, import the module (extend line 23-26 imports):

```python
from data_aggregator_mcp import croissant as croissant_mod
```

Add a `format` property to the `resolve` tool inputSchema (inside its `properties`, alongside `cite`):

```python
                "format": {
                    "type": "string",
                    "enum": ["croissant"],
                    "description": "Optional export to render onto the result. 'croissant' "
                    "attaches a file-level Croissant JSON-LD manifest (croissant field).",
                },
```

In `_dispatch` `resolve` case (after the `cite` block, before `return resource.model_dump()`, line 386):

```python
                fmt = args.get("format")
                if fmt == "croissant":
                    resource = resource.model_copy(
                        update={"croissant": croissant_mod.render(resource)}
                    )
```

- [ ] **Step 6: Write + run the dispatch test**

Append to `tests/test_server.py`:

```python
async def test_dispatch_resolve_renders_croissant(monkeypatch) -> None:
    from data_aggregator_mcp.models import DataResource, FileEntry

    async def fake_resolve(client, fid):
        return DataResource(
            id="zenodo:1", source="zenodo", kind="dataset", title="t",
            files=[FileEntry(name="a.csv", url="https://x/a.csv")],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1", "format": "croissant"})
    assert out["croissant"]["@type"] == "Dataset"
    assert out["croissant"]["distribution"][0]["name"] == "a.csv"
```

Run: `pytest tests/test_server.py::test_dispatch_resolve_renders_croissant tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/data_aggregator_mcp/croissant.py src/data_aggregator_mcp/models.py src/data_aggregator_mcp/server.py tests/test_croissant.py tests/test_server.py
git commit -m "feat: file-level Croissant export via resolve(format=croissant)"
```

---

### Task 10: RO-Crate export

Same pattern; extends the `resolve` `format` enum and adds a nullable `ro_crate` field. Fits our Zenodo/Dryad/omics research-output corpus.

**Files:**

- Create: `src/data_aggregator_mcp/ro_crate.py`
- Modify: `src/data_aggregator_mcp/models.py` (1 field)
- Modify: `src/data_aggregator_mcp/server.py` (`format` enum + dispatch branch)
- Test: `tests/test_ro_crate.py` (create), `tests/test_server.py`

- [ ] **Step 1: Write the failing renderer test**

Create `tests/test_ro_crate.py`:

```python
from data_aggregator_mcp import ro_crate
from data_aggregator_mcp.models import Creator, DataResource, FileEntry


def _resource() -> DataResource:
    return DataResource(
        id="zenodo:1", source="zenodo", kind="dataset", title="Rice genomes",
        description="d", doi="10.5281/zenodo.1", creators=[Creator(name="A. Author")],
        year=2024, license="cc-by-4.0",
        files=[FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=10)],
    )


def test_render_produces_ro_crate_graph() -> None:
    c = ro_crate.render(_resource())
    assert c["@context"] == "https://w3id.org/ro/crate/1.1/context"
    graph = {e["@id"]: e for e in c["@graph"]}
    assert graph["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    root = graph["./"]
    assert root["@type"] == "Dataset"
    assert root["name"] == "Rice genomes"
    assert {"@id": "https://x/a.csv"} in root["hasPart"]
    assert graph["https://x/a.csv"]["@type"] == "File"
    assert graph["https://x/a.csv"]["encodingFormat"] == "text/csv"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_ro_crate.py -v`
Expected: FAIL (`No module named 'data_aggregator_mcp.ro_crate'`).

- [ ] **Step 3: Implement the renderer**

Create `src/data_aggregator_mcp/ro_crate.py`:

```python
"""RO-Crate export — minimal RO-Crate 1.1 metadata for a resolved resource.

Renders the root data entity + its files as an RO-Crate @graph. Pure transform.
Complements the Croissant export: RO-Crate is the research-output packaging
standard (general datasets, software, papers), Croissant the ML-dataset one.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp.models import DataResource

CONTEXT = "https://w3id.org/ro/crate/1.1/context"
CONFORMS_TO = "https://w3id.org/ro/crate/1.1"


def render(r: DataResource) -> dict[str, Any]:
    root: dict[str, Any] = {"@id": "./", "@type": "Dataset", "name": r.title}
    if r.description:
        root["description"] = r.description
    if r.doi:
        root["identifier"] = f"https://doi.org/{r.doi}"
    if r.license:
        root["license"] = r.license
    if r.year:
        root["datePublished"] = str(r.year)
    if r.creators:
        root["author"] = [{"@type": "Person", "name": c.name} for c in r.creators]

    file_entities: list[dict[str, Any]] = []
    has_part: list[dict[str, str]] = []
    for f in r.files:
        fid = f.url or f.name
        has_part.append({"@id": fid})
        ent: dict[str, Any] = {"@id": fid, "@type": "File", "name": f.name}
        if f.mime:
            ent["encodingFormat"] = f.mime
        if f.size is not None:
            ent["contentSize"] = f.size
        file_entities.append(ent)
    root["hasPart"] = has_part

    return {
        "@context": CONTEXT,
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "conformsTo": {"@id": CONFORMS_TO},
                "about": {"@id": "./"},
            },
            root,
            *file_entities,
        ],
    }
```

- [ ] **Step 4: Run the renderer test**

Run: `pytest tests/test_ro_crate.py -v`
Expected: PASS.

- [ ] **Step 5: Add the field + wire the format branch**

In `models.py`, add to `DataResource` (after `croissant`):

```python
    ro_crate: dict[str, Any] | None = None  # RO-Crate export, on resolve(format=ro-crate)
```

In `server.py`, import: extend the croissant import line with:

```python
from data_aggregator_mcp import ro_crate as ro_crate_mod
```

Change the `resolve` tool `format` enum to `["croissant", "ro-crate"]` and update its description to mention RO-Crate. In `_dispatch`, extend the format block:

```python
                fmt = args.get("format")
                if fmt == "croissant":
                    resource = resource.model_copy(
                        update={"croissant": croissant_mod.render(resource)}
                    )
                elif fmt == "ro-crate":
                    resource = resource.model_copy(
                        update={"ro_crate": ro_crate_mod.render(resource)}
                    )
```

- [ ] **Step 6: Write + run the dispatch test**

Append to `tests/test_server.py`:

```python
async def test_dispatch_resolve_renders_ro_crate(monkeypatch) -> None:
    from data_aggregator_mcp.models import DataResource, FileEntry

    async def fake_resolve(client, fid):
        return DataResource(
            id="zenodo:1", source="zenodo", kind="dataset", title="t",
            files=[FileEntry(name="a.csv", url="https://x/a.csv")],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1", "format": "ro-crate"})
    assert out["ro_crate"]["@context"] == "https://w3id.org/ro/crate/1.1/context"
```

Run: `pytest tests/test_ro_crate.py tests/test_server.py::test_dispatch_resolve_renders_ro_crate tests/test_output_schema_gate.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/data_aggregator_mcp/ro_crate.py src/data_aggregator_mcp/models.py src/data_aggregator_mcp/server.py tests/test_ro_crate.py tests/test_server.py
git commit -m "feat: RO-Crate export via resolve(format=ro-crate)"
```

---

### Task 11: Version bump to 0.17.0 + CHANGELOG + full suite

**Files:**

- Modify: `pyproject.toml:3`, `src/data_aggregator_mcp/__init__.py`, `server.json` (top-level `version` + `packages[0].version`)
- Modify: `CHANGELOG.md`
- Verify: `tests/test_packaging.py`

- [ ] **Step 1: Read the three version sites + the packaging test**

Run: `grep -n '0\.16\.0' pyproject.toml src/data_aggregator_mcp/__init__.py server.json && sed -n '1,40p' tests/test_packaging.py`
Confirm exactly where `0.16.0` appears (PUBLISH.md documents these as the four sync points: pyproject, `__init__`, and `server.json`'s two version fields).

- [ ] **Step 2: Set every site to 0.17.0**

Edit each occurrence found in Step 1 from `0.16.0` to `0.17.0` (pyproject `version`, `__version__`, server.json top-level `version`, server.json `packages[0].version`).

- [ ] **Step 3: Append the CHANGELOG section**

Add to the top of `CHANGELOG.md` (under any header, above 0.16.0):

```markdown
## 0.17.0

### Added

- Structured-output round-trip gate (`tests/test_output_schema_gate.py`) — every tool's output is validated against its declared `outputSchema`, guarding against field drift.
- `DataResource.metrics` (citations/views/downloads/likes — separate axes, no blended score), populated from DataCite inline counts and HuggingFace downloads/likes.
- `DataResource.is_latest` / `superseded_by`, derived from version relations in `links[]` (fields only; no ranking change).
- `DataResource.last_updated` freshness (DataCite + HuggingFace).
- Tool annotations (`readOnlyHint` on search/resolve/list_sources; explicit hints on fetch).
- MCP prompts: `find_data`, `data_behind_paper`, `search_resolve_fetch`.
- Export: `resolve(format="croissant")` (file-level Croissant) and `resolve(format="ro-crate")` (RO-Crate 1.1).
```

- [ ] **Step 4: Run the whole suite**

Run: `pytest -q`
Expected: all PASS (including `test_packaging.py` version assertions).

- [ ] **Step 5: Run lint**

Run: `ruff check src tests && ruff format --check src tests`
Expected: clean (fix any findings, re-run).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/data_aggregator_mcp/__init__.py server.json CHANGELOG.md
git commit -m "chore: bump to 0.17.0 (Phase 1 MCP-citizen + trust + interop)"
```

---

## Distribution track (parallel, non-TDD — outward-facing actions)

These are external/manual actions, not unit-testable code. Reference: `~/.claude/projects/-home-mjarnold/memory/mcp_directory_listings_scope_2026-05-30.md`. **Each one publishes to a third party — run only with explicit user go-ahead** (consistent with `PUBLISH.md` being manual). Surface a draft for review before submitting.

- [ ] **D1: `glama.json` at repo root** — create the Glama manifest (maintainers + categories). Draft, show the user, commit. Then claim the server on glama.ai.
- [ ] **D2: biocontext.ai listing** — open a PR adding the server's `meta.yaml` (bio audience = best ROI). Draft the PR locally; user submits.
- [ ] **D3: Smithery + mcp.so** — submit/claim the published server (already on the official registry → some directories auto-ingest; verify and fill gaps).
- [ ] **D4: positioning writeup** — add a short "Why this vs. a gateway" section to `README.md` leading with the three uncontested differentiators (normalized cross-source dedup + NCBI-Taxonomy synonym expansion + bidirectional paper↔data bridge). This is the launch narrative the competitive memo calls the #1 lever.

---

## Self-Review

**Spec coverage (vs master plan §Resolved sequencing, Phase 1 "now" wave):**

- schema round-trip gate (prereq #1) → Task 1 ✓
- metrics (Q5: typed, separate axes, no blended score) → Tasks 2-4 ✓
- is_latest/superseded_by (Q6: fields only, no ranking) → Task 5 ✓
- last_updated → Task 6 (datacite+hf; rest deferred, noted) ✓
- annotations → Task 7 ✓; prompts → Task 8 ✓
- Croissant file-level (not RecordSet — deferred to post-P2.4) → Task 9 ✓; RO-Crate → Task 10 ✓
- distribution pulled forward (Q1) → Distribution track ✓
- version/CHANGELOG → Task 11 ✓
- relationType/ROR (P1.9) → explicitly deferred (OUT section) ✓

**Placeholder scan:** no TBD/"handle edge cases"/"similar to Task N" — every code step shows complete code. ✓

**Type consistency:** `Metrics(citations,views,downloads,likes)` used identically in Tasks 2/3/4 + gate. `derive_version_status(links) -> (bool|None, str|None)` used in Task 5 model + both router call sites. `croissant.render(r)->dict` and `ro_crate.render(r)->dict` match their dispatch calls. New fields (`metrics, is_latest, superseded_by, last_updated, croissant, ro_crate`) are all nullable/defaulted → the schema gate stays green at every step. ✓

**Ordering safety:** Task 1 lands the gate before any field; each field task re-runs it. Tasks 9/10 reuse the `citation`-style `format` arg without touching `resolve`'s outputSchema shape (fields are additive). ✓
