# B9 `relate` — Join/Harmonization Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 6th MCP tool `relate(ids)` that emits evidence-backed, metadata-level join/harmonization HINTS across a set of resolved resources — without reading files, comparing columns, or executing joins.

**Architecture:** Pure deterministic `relate.detect(resources) -> list[JoinHint]` (no I/O), with the resolve fan-out + assembly in a `router.relate(client, ids)` handler — mirroring the `dossier.py` pure-renderer + I/O-in-handler split. Four detectors (shared accession, shared cross-identifier, explicit link, version lineage). New `JoinHint` / `RelateResult` models. New tool registered + dispatched in `server.py`.

**Tech Stack:** Python 3.11+, Pydantic v2 models, `pytest`/`pytest-asyncio`, `httpx.AsyncClient`, `asyncio.gather`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-11-b9-relate-join-hints-design.md`

---

### Task 1: Models — `JoinHint` and `RelateResult`

**Files:**

- Modify: `src/data_aggregator_mcp/models.py` (add two classes near `SearchResult`)
- Test: `tests/test_relate.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_relate.py`:

```python
from __future__ import annotations

from data_aggregator_mcp.models import JoinHint, RelateResult


def test_joinhint_and_relateresult_construct() -> None:
    h = JoinHint(
        kind="shared_accession",
        resources=["geo:GSE1", "sra:SRP1"],
        key="PRJNA1",
        evidence="accession 'PRJNA1' present on 2 resources",
        suggestion="joinable on accession PRJNA1",
    )
    r = RelateResult(input_ids=["geo:GSE1", "sra:SRP1"], resolved=["geo:GSE1", "sra:SRP1"], hints=[h])
    assert r.hints[0].kind == "shared_accession"
    assert r.errors == {}
    assert r.note is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relate.py::test_joinhint_and_relateresult_construct -v`
Expected: FAIL with `ImportError: cannot import name 'JoinHint'`.

- [ ] **Step 3: Add the models**

In `src/data_aggregator_mcp/models.py`, immediately BEFORE `class SearchResult(BaseModel):` add:

```python
class JoinHint(BaseModel):
    """One evidence-backed cross-resource relationship hint (B9 `relate`). Metadata-level
    only — names a shared value/relation; never an executed join."""

    kind: Literal["shared_accession", "shared_identifier", "explicit_link", "version_lineage"]
    resources: list[str]  # >=2 distinct input resource ids this hint connects
    key: str  # the shared value or relation (the accession, the doi/pmid, the link rel)
    evidence: str  # what was matched and where
    suggestion: str  # human/agent-readable HINT; never an executed action


class RelateResult(BaseModel):
    """Result of `relate(ids)` — metadata-level join/harmonization hints across a resource
    set. `note` is set (and `hints` empty) when nothing structural was found, distinguishing
    'looked and found nothing' from an error."""

    input_ids: list[str]
    resolved: list[str]  # canonical ids that resolved successfully
    hints: list[JoinHint] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)  # input id -> resolve-failure reason
    note: str | None = None
```

Confirm `Literal` and `Field` are already imported at the top of `models.py` (they are — `LicenseVerdict` uses `Literal`, many models use `Field`). If not, add `from typing import Literal` / import `Field` from pydantic.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_relate.py::test_joinhint_and_relateresult_construct -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/models.py tests/test_relate.py
git commit -m "feat(b9): JoinHint + RelateResult models"
```

---

### Task 2: `relate.detect` scaffold + `shared_accession` detector

**Files:**

- Create: `src/data_aggregator_mcp/relate.py`
- Test: `tests/test_relate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relate.py`:

```python
from data_aggregator_mcp import relate as relate_mod
from data_aggregator_mcp.models import DataResource


def _res(rid: str, **kw) -> DataResource:
    # minimal valid DataResource; required fields: id, source, kind, title
    base = dict(id=rid, source=rid.split(":")[0], kind="dataset", title=rid)
    base.update(kw)
    return DataResource(**base)


def test_shared_accession_collapses_to_one_hint() -> None:
    rs = [
        _res("geo:GSE1", accessions=["PRJNA1"]),
        _res("sra:SRP1", accessions=["prjna1"]),   # case-insensitive match
        _res("zenodo:9", accessions=["PRJNA1"]),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "shared_accession"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"geo:GSE1", "sra:SRP1", "zenodo:9"}
    assert hints[0].key == "PRJNA1"


def test_no_accession_hint_when_unshared() -> None:
    rs = [_res("geo:GSE1", accessions=["PRJNA1"]), _res("sra:SRP1", accessions=["PRJNA2"])]
    assert [h for h in relate_mod.detect(rs) if h.kind == "shared_accession"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relate.py -k shared_accession -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'data_aggregator_mcp.relate'`.

- [ ] **Step 3: Create `relate.py` with the scaffold + accession detector**

Create `src/data_aggregator_mcp/relate.py`:

```python
"""B9 `relate` — pure, deterministic cross-resource join/harmonization hint detection.

`detect(resources)` reasons over the NORMALIZED metadata of already-resolved
DataResources and returns evidence-backed JoinHints for four strong structural signals.
NO network, NO file I/O, NO executed joins — it names a shared value and stops (the
HINTS-only boundary). The handler (`router.relate`) does the resolve fan-out.
"""

from __future__ import annotations

from data_aggregator_mcp.models import DataResource, JoinHint


def _norm(value: str | None) -> str | None:
    """Case-insensitive, stripped key for exact-match comparison; None if empty."""
    if not value:
        return None
    s = value.strip().lower()
    return s or None


def detect(resources: list[DataResource]) -> list[JoinHint]:
    """All hints across `resources`. Order: accession, identifier, link, lineage."""
    hints: list[JoinHint] = []
    hints.extend(_shared_accession(resources))
    return hints


def _shared_accession(resources: list[DataResource]) -> list[JoinHint]:
    by_acc: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for r in resources:
        for acc in r.accessions:
            n = _norm(acc)
            if not n:
                continue
            display.setdefault(n, acc)
            ids = by_acc.setdefault(n, [])
            if r.id not in ids:
                ids.append(r.id)
    hints: list[JoinHint] = []
    for n, ids in by_acc.items():
        if len(ids) >= 2:
            hints.append(
                JoinHint(
                    kind="shared_accession",
                    resources=ids,
                    key=display[n],
                    evidence=f"accession {display[n]!r} present on {len(ids)} resources",
                    suggestion=f"joinable on accession {display[n]}",
                )
            )
    return hints
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_relate.py -k shared_accession -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/relate.py tests/test_relate.py
git commit -m "feat(b9): relate.detect scaffold + shared_accession detector"
```

---

### Task 3: `shared_identifier` detector

**Files:**

- Modify: `src/data_aggregator_mcp/relate.py`
- Test: `tests/test_relate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relate.py`:

```python
def test_shared_identifier_across_doi_and_identifiers() -> None:
    rs = [
        _res("pubmed:1", identifiers={"doi": "10.1/x", "pmid": "1"}),
        _res("zenodo:2", doi="10.1/X"),  # same doi, different case
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "shared_identifier"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"pubmed:1", "zenodo:2"}
    assert hints[0].key in ("10.1/x", "10.1/X")


def test_no_self_identifier_hint() -> None:
    # one resource carrying its own doi in both `doi` and `identifiers` must not self-hint
    rs = [_res("zenodo:2", doi="10.1/x", identifiers={"doi": "10.1/x"})]
    assert [h for h in relate_mod.detect(rs) if h.kind == "shared_identifier"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relate.py -k shared_identifier -v`
Expected: FAIL (no `shared_identifier` hints produced yet).

- [ ] **Step 3: Add the detector + wire it into `detect`**

In `src/data_aggregator_mcp/relate.py`, add `hints.extend(_shared_identifier(resources))` to `detect` (after the accession line), and add:

```python
def _shared_identifier(resources: list[DataResource]) -> list[JoinHint]:
    by_id: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for r in resources:
        values: set[str] = set()
        if r.doi:
            values.add(r.doi)
        for v in r.identifiers.values():
            if v:
                values.add(v)
        for v in values:
            n = _norm(v)
            if not n:
                continue
            display.setdefault(n, v)
            ids = by_id.setdefault(n, [])
            if r.id not in ids:  # one resource counts once per value -> no self-hint
                ids.append(r.id)
    hints: list[JoinHint] = []
    for n, ids in by_id.items():
        if len(ids) >= 2:
            hints.append(
                JoinHint(
                    kind="shared_identifier",
                    resources=ids,
                    key=display[n],
                    evidence=f"identifier {display[n]!r} shared by {len(ids)} resources",
                    suggestion=f"same work or paper-data link via {display[n]}",
                )
            )
    return hints
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_relate.py -k shared_identifier -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/relate.py tests/test_relate.py
git commit -m "feat(b9): shared_identifier detector"
```

---

### Task 4: `explicit_link` detector

**Files:**

- Modify: `src/data_aggregator_mcp/relate.py`
- Test: `tests/test_relate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relate.py`:

```python
from data_aggregator_mcp.models import Link


def test_explicit_link_target_matches_another_resource() -> None:
    rs = [
        _res("pubmed:1", links=[Link(rel="describes", target_id="zenodo:2")]),
        _res("zenodo:2"),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "explicit_link"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"pubmed:1", "zenodo:2"}
    assert hints[0].key == "describes"


def test_explicit_link_to_outside_id_is_ignored() -> None:
    rs = [_res("pubmed:1", links=[Link(rel="describes", target_id="zenodo:999")]), _res("zenodo:2")]
    assert [h for h in relate_mod.detect(rs) if h.kind == "explicit_link"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relate.py -k explicit_link -v`
Expected: FAIL (no `explicit_link` hints yet).

- [ ] **Step 3: Add the detector + wire it into `detect`**

In `relate.py`, add `hints.extend(_explicit_link(resources))` to `detect`, and add:

```python
def _address_map(resources: list[DataResource], *, include_accessions: bool) -> dict[str, str]:
    """Map each resource's addressable ids (id, doi, optionally accessions), normalized,
    to the OWNING resource id. First writer wins on a collision (shared doi is handled by
    the identifier detector, not here)."""
    addr: dict[str, str] = {}
    for r in resources:
        candidates = [r.id, r.doi]
        if include_accessions:
            candidates += list(r.accessions)
        for c in candidates:
            n = _norm(c)
            if n:
                addr.setdefault(n, r.id)
    return addr


def _explicit_link(resources: list[DataResource]) -> list[JoinHint]:
    addr = _address_map(resources, include_accessions=True)
    hints: list[JoinHint] = []
    seen: set[tuple[str, str, str]] = set()
    for r in resources:
        for link in r.links:
            n = _norm(link.target_id)
            if not n:
                continue
            target = addr.get(n)
            if not target or target == r.id:
                continue
            dedup = (r.id, target, link.rel)
            if dedup in seen:
                continue
            seen.add(dedup)
            hints.append(
                JoinHint(
                    kind="explicit_link",
                    resources=[r.id, target],
                    key=link.rel,
                    evidence=f"{r.id} links to {target} via {link.rel!r} (target_id={link.target_id!r})",
                    suggestion=f"{r.id} {link.rel} {target} (declared in source metadata)",
                )
            )
    return hints
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_relate.py -k explicit_link -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/relate.py tests/test_relate.py
git commit -m "feat(b9): explicit_link detector"
```

---

### Task 5: `version_lineage` detector + cross-signal negative

**Files:**

- Modify: `src/data_aggregator_mcp/relate.py`
- Test: `tests/test_relate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relate.py`:

```python
def test_version_lineage_directed_edge() -> None:
    rs = [
        _res("zenodo:1", superseded_by="zenodo:2"),  # 1 is older, points to newer 2
        _res("zenodo:2"),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "version_lineage"]
    assert len(hints) == 1
    assert hints[0].resources == ["zenodo:2", "zenodo:1"]  # [newer, older]
    assert "newer version" in hints[0].suggestion


def test_no_hint_on_shared_organism_only() -> None:
    rs = [_res("zenodo:1", organism=["human"]), _res("zenodo:2", organism=["human"])]
    assert relate_mod.detect(rs) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_relate.py -k "version_lineage or shared_organism" -v`
Expected: `test_version_lineage_directed_edge` FAILs (no lineage hints yet); `test_no_hint_on_shared_organism_only` may already pass.

- [ ] **Step 3: Add the detector + wire it into `detect`**

In `relate.py`, add `hints.extend(_version_lineage(resources))` to `detect`, and add:

```python
def _version_lineage(resources: list[DataResource]) -> list[JoinHint]:
    addr = _address_map(resources, include_accessions=False)
    hints: list[JoinHint] = []
    seen: set[tuple[str, str]] = set()
    for r in resources:
        n = _norm(r.superseded_by)
        if not n:
            continue
        newer = addr.get(n)  # the resource r.superseded_by points to
        if not newer or newer == r.id:
            continue
        dedup = tuple(sorted((r.id, newer)))
        if dedup in seen:
            continue
        seen.add(dedup)
        hints.append(
            JoinHint(
                kind="version_lineage",
                resources=[newer, r.id],  # [newer, older]
                key=newer,
                evidence=f"{r.id}.superseded_by -> {newer}",
                suggestion=f"{newer} is a newer version of {r.id} - dedupe, don't join, these",
            )
        )
    return hints
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_relate.py -v`
Expected: PASS (all `relate` unit tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/relate.py tests/test_relate.py
git commit -m "feat(b9): version_lineage detector + cross-signal negative"
```

---

### Task 6: `router.relate` handler (resolve fan-out, fail-soft, guards)

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (import + new `async def relate`)
- Test: `tests/test_router.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_router.py` (it already imports `router`, `httpx`, `AsyncMock`, `DataResource`, `ValidationError` — if `ValidationError` is not imported there, add `from data_aggregator_mcp.errors import ValidationError`):

```python
async def test_relate_resolves_and_returns_hints(monkeypatch) -> None:
    from data_aggregator_mcp.models import DataResource as DR

    async def fake_resolve(client, rid):
        return {
            "geo:GSE1": DR(id="geo:GSE1", source="geo", kind="dataset", title="a", accessions=["PRJNA1"]),
            "sra:SRP1": DR(id="sra:SRP1", source="sra", kind="dataset", title="b", accessions=["PRJNA1"]),
        }[rid]

    monkeypatch.setattr(router, "resolve", fake_resolve)
    async with httpx.AsyncClient() as client:
        out = await router.relate(client, ["geo:GSE1", "sra:SRP1"])
    assert out.resolved == ["geo:GSE1", "sra:SRP1"]
    assert len(out.hints) == 1 and out.hints[0].kind == "shared_accession"
    assert out.errors == {} and out.note is None


async def test_relate_fail_soft_on_one_bad_id(monkeypatch) -> None:
    from data_aggregator_mcp.models import DataResource as DR

    async def fake_resolve(client, rid):
        if rid == "bad:1":
            raise LookupError("not found")
        return DR(id=rid, source="zenodo", kind="dataset", title="t")

    monkeypatch.setattr(router, "resolve", fake_resolve)
    async with httpx.AsyncClient() as client:
        out = await router.relate(client, ["zenodo:1", "bad:1"])
    assert "bad:1" in out.errors
    assert out.resolved == ["zenodo:1"]
    assert out.hints == []
    assert out.note is not None  # fewer than 2 resolved


async def test_relate_count_guards(monkeypatch) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValidationError):
            await router.relate(client, ["only:1"])
        with pytest.raises(ValidationError):
            await router.relate(client, [f"x:{i}" for i in range(11)])
```

(`pytest` is already imported in `tests/test_router.py`; if not, add `import pytest`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router.py -k relate -v`
Expected: FAIL with `AttributeError: module 'data_aggregator_mcp.router' has no attribute 'relate'`.

- [ ] **Step 3: Implement `router.relate`**

In `src/data_aggregator_mcp/router.py`: add `from data_aggregator_mcp import relate as relate_mod` near the other `import ... as ..._mod` lines (e.g. after line 43), and add `RelateResult` to the `from data_aggregator_mcp.models import (...)` block (line 47). Then add at the end of the file:

```python
RELATE_MAX_IDS = 10


async def relate(client: httpx.AsyncClient, ids: list[str]) -> RelateResult:
    """Resolve `ids` (TTL-cached, concurrent, fail-soft) and return metadata-level
    join/harmonization hints. 2..RELATE_MAX_IDS ids; <2 or >max -> ValidationError."""
    if not isinstance(ids, list) or len(ids) < 2:
        raise ValidationError("relate needs at least 2 ids")
    if len(ids) > RELATE_MAX_IDS:
        raise ValidationError(f"relate accepts at most {RELATE_MAX_IDS} ids; got {len(ids)}")

    settled = await asyncio.gather(*(resolve(client, i) for i in ids), return_exceptions=True)
    resolved: list[DataResource] = []
    resolved_ids: list[str] = []
    errors: dict[str, str] = {}
    for given, res in zip(ids, settled):
        if isinstance(res, Exception):
            errors[given] = f"{type(res).__name__}: {res}"
        else:
            resolved.append(res)
            resolved_ids.append(res.id)

    hints = relate_mod.detect(resolved) if len(resolved) >= 2 else []
    note: str | None = None
    if len(resolved) < 2:
        note = f"fewer than 2 ids resolved ({len(resolved)}); need 2+ to compare"
    elif not hints:
        note = f"no structural relationships detected among {len(resolved)} resources"
    return RelateResult(
        input_ids=ids, resolved=resolved_ids, hints=hints, errors=errors, note=note
    )
```

Note: `DataResource` is already imported in `router.py`'s models block; confirm it is in the import list (it is used throughout).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_router.py -k relate -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py tests/test_router.py
git commit -m "feat(b9): router.relate handler — resolve fan-out, fail-soft, guards"
```

---

### Task 7: Register + dispatch the `relate` tool in `server.py`

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (TOOLS list + `_dispatch` match)
- Test: `tests/test_server.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py`:

```python
async def test_relate_tool_dispatch(monkeypatch) -> None:
    from data_aggregator_mcp import server
    from data_aggregator_mcp.models import DataResource as DR

    async def fake_resolve(client, rid):
        return DR(id=rid, source="geo", kind="dataset", title="t", accessions=["PRJNA9"])

    monkeypatch.setattr(server.router, "resolve", fake_resolve)
    out = await server._dispatch("relate", {"ids": ["geo:GSE1", "sra:SRP1"]})
    assert out["resolved"] == ["geo:GSE1", "sra:SRP1"]
    assert out["hints"][0]["kind"] == "shared_accession"


def test_relate_is_registered() -> None:
    from data_aggregator_mcp import server

    tool = next(t for t in server.TOOLS if t.name == "relate")
    assert tool.inputSchema["required"] == ["ids"]
    assert tool.inputSchema["properties"]["ids"]["minItems"] == 2
    assert tool.inputSchema["properties"]["ids"]["maxItems"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py -k relate -v`
Expected: FAIL — `_dispatch` raises `ValueError: unknown tool: relate`, and `next(...)` raises `StopIteration`.

- [ ] **Step 3a: Register the tool**

In `src/data_aggregator_mcp/server.py`, add a new `types.Tool(...)` entry to the `TOOLS` list, immediately after the `operate` tool (after its closing `),` near line 677, before the list-closing `]`):

```python
    types.Tool(
        name="relate",
        description=(
            "Given 2-10 resource ids, return metadata-level join/harmonization HINTS: how "
            "the datasets relate and on what key they could be joined. Detects shared "
            "accessions (BioProject/SRA/GEO), shared cross-identifiers (doi/pmid/pmcid), "
            "explicit links between the inputs, and version lineage. HINTS ONLY — it does "
            "not read file columns, fetch files, or execute any join/merge/conversion; each "
            "hint names the shared value as evidence. Resolve ids first if you only have a "
            "search result. Per-id resolve failures are reported, not fatal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 10,
                    "description": "2-10 source-prefixed resource ids to relate.",
                },
            },
            "required": ["ids"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
```

- [ ] **Step 3b: Dispatch the tool**

In `_dispatch`, add a case immediately after the `case "operate":` block (before `case _:`):

```python
            case "relate":
                result = await router.relate(client, args["ids"])
                return result.model_dump()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py -k relate -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(b9): register + dispatch the relate tool"
```

---

### Task 8: Full suite + lint gate

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (prior count 871 + the new `relate` tests), only the known skips.

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/ tests/`
Expected: `All checks passed!`

- [ ] **Step 3: Commit (only if lint auto-fixed anything; otherwise skip)**

```bash
git add -A && git commit -m "chore(b9): lint" || true
```

---

### Task 9: Real-execution check against live ids

**Files:**

- Create: `scripts/relate_live_check.py` (a throwaway live probe; keep it — it documents the verified pair)

- [ ] **Step 1: Discover a real related pair (NO fabricated ids)**

Write `scripts/relate_live_check.py`:

```python
import asyncio
import os
import sys

import httpx

from data_aggregator_mcp import router

# Discover live: search omics for a topic, resolve the top hits, and relate them. We
# look for a GEO/SRA pair that shares a BioProject accession OR a record that links to
# another. The exact ids are NOT hard-coded — they are discovered at runtime so the
# check stays honest (same discipline as the recall-eval anchors).


async def main() -> int:
    if os.environ.get("DATA_AGGREGATOR_MCP_LIVE") != "1":
        print("SKIP: set DATA_AGGREGATOR_MCP_LIVE=1 to run the live check.")
        return 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await router.search_page(client, query="RNA-seq", size=10, sources=["geo"])
        ids = [r.id for r in res.results][:6]
        print("candidate ids:", ids)
        if len(ids) < 2:
            print("FAIL: could not find >=2 live ids")
            return 1
        out = await router.relate(client, ids)
        print("resolved:", out.resolved)
        print("errors:", out.errors)
        for h in out.hints:
            print(f"HINT {h.kind}: {h.resources} key={h.key!r} :: {h.suggestion}")
        print("note:", out.note)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run it live**

Run: `DATA_AGGREGATOR_MCP_LIVE=1 uv run python scripts/relate_live_check.py`
Expected: prints resolved ids + any hints. GEO series frequently carry a BioProject/SRA accession, so a `shared_accession` or `explicit_link` hint is likely. If the first topic yields no related pair, try another query (e.g. `sources=["geo"]` with a more specific topic, or include `["sra"]`) until at least one real hint is produced — then record the producing ids in a comment at the top of the script. This verifies `relate` end-to-end against the live resolve path, not mocks.

- [ ] **Step 3: Commit the verified probe**

```bash
git add scripts/relate_live_check.py
git commit -m "test(b9): live relate check against discovered real ids"
```

---

### Task 10: Docs — README + CHANGELOG

**Files:**

- Modify: `README.md` (tool list)
- Modify: `CHANGELOG.md` (Unreleased -> new version section is done at release time; add under `[Unreleased]` for now)

- [ ] **Step 1: Document `relate` in the README tool list**

In `README.md`, after the `operate(...)` tool subsection (find the `### ` heading for operate; add a sibling section after it):

```markdown
### `relate(ids)`

Cross-resource join/harmonization **hints**. Given 2–10 resource ids, `relate` resolves
each (TTL-cached) and reports how they relate and on what key they could be joined:

- **`shared_accession`** — same BioProject/SRA/GEO accession on ≥2 records → joinable key.
- **`shared_identifier`** — same doi/pmid/pmcid across records → same work / paper↔data link.
- **`explicit_link`** — one record's `links[]` points at another input record.
- **`version_lineage`** — one record supersedes another (dedupe, don't join, those).

**Hints only.** `relate` never reads file columns, fetches files, or executes a
join/merge/conversion — every hint names the shared value as evidence. Per-id resolve
failures are reported in `errors`, not fatal; an empty result carries an explanatory
`note`.
```

- [ ] **Step 2: Add a CHANGELOG entry under `[Unreleased]`**

In `CHANGELOG.md`, under the `## [Unreleased]` heading, add:

```markdown
### Added

- **`relate(ids)` — cross-resource join/harmonization hints (B9).** A 6th tool: given
  2–10 resource ids, it resolves each (cached) and emits evidence-backed, metadata-level
  hints — shared accession (BioProject/SRA/GEO), shared cross-identifier (doi/pmid/pmcid),
  explicit link between inputs, and version lineage. HINTS ONLY: no file reads, no column
  comparison, no executed joins. Per-id resolve failures are reported, not fatal.
```

- [ ] **Step 3: Verify docs build / no broken markdown**

Run: `uv run pytest tests/test_packaging.py -q`
Expected: PASS (README ownership marker test still green).

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs(b9): document relate tool in README + CHANGELOG"
```

---

## Release (after all tasks pass — separate confirmation)

Not a task in this plan. When B9 is merged and green, cut `v0.39.0` the same way as v0.38.0:
bump `pyproject.toml` + `__init__.py` + `server.json` (×2) + `tests/test_packaging.py`
version assertions, move the CHANGELOG `[Unreleased]` block under `## [0.39.0] - <date>`,
push, lightweight trailer-free tag, `gh release create`, watch both publish workflows,
verify PyPI 200 + registry `version=latest`.

---

## Self-Review

**Spec coverage:**

- Metadata-level only → no file/column code anywhere ✓ (Tasks 2–5 are pure metadata).
- New tool, explicit ids, resolves internally → Task 6 + 7 ✓.
- 4 strong signals → Tasks 2,3,4,5 ✓.
- 2–10 cap, fail-soft, `note` when empty → Task 6 ✓.
- `JoinHint`/`RelateResult` models → Task 1 ✓.
- Pure `detect` + I/O-in-handler split → `relate.py` pure (Tasks 2–5), `router.relate` does I/O (Task 6) ✓.
- HINTS-only boundary → enforced by construction (no fetch/column/join calls) + asserted in tool description/README/CHANGELOG ✓.
- Unit tests per signal + negatives (organism-only, self-id) → Tasks 2,3,5 ✓.
- Handler fail-soft + count guards tests → Task 6 ✓.
- Real-execution check with discovered live ids → Task 9 ✓.
- README + CHANGELOG → Task 10 ✓.

**Placeholder scan:** every code step shows complete code; no TBD/TODO; test code is concrete. ✓

**Type consistency:** `detect(resources) -> list[JoinHint]` used identically in Tasks 2–6; `JoinHint` fields (`kind/resources/key/evidence/suggestion`) and `RelateResult` fields (`input_ids/resolved/hints/errors/note`) match Task 1's definitions throughout; `router.relate` returns `RelateResult` consumed via `.model_dump()` in Task 7; `_address_map(..., include_accessions=bool)` defined in Task 4 and reused in Task 5. ✓
