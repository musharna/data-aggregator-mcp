# P2 — Relations + ORCID + Funding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend `DataResource` with structured creators (name+ORCID), funding refs, and
related-identifier links; ship v0.13.0. Spec:
`docs/superpowers/specs/2026-05-31-p2-relations-orcid-funding-design.md`.

**Architecture:** Two new model types (`Creator`, `FundingRef`) + a shared `_orcid` normalizer
and a `_rel` snake-caser. The four creator-producing adapters (`datacite`, `zenodo`, `openaire`,
`pubmed`) map authors → `Creator`; `datacite`/`zenodo` additionally extract funding + related
links from metadata they already fetch. No new tools, no traversal, no extra network calls.

**Conventions (verified live):** `creators` is consumed ONLY at `citation.py:48`. Creator-producing
sites: `datacite.py:121`, `zenodo.py:53`, `openaire.py:67`, `pubmed.py:48`. `Link{rel,target_id}`
exists; `links` is not stripped by `compact`. Version synced across `pyproject.toml:3`,
`__init__.py:3`, `server.json` ×2. A PostToolUse formatter reflows files after each write — re-Read
before a second Edit to the same region; fresh-read-guard blocks edits to unread files. Commit
trailer (exactly): `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

**IMPORTANT — Task 1 is an atomic breaking change.** Changing `creators` from `list[str]` to
`list[Creator]` breaks every adapter that builds it + `citation.py` + their tests at once. Task 1
MUST land the model change, all 4 adapters' creator mapping (name only, no ORCID yet), the
`citation.py` fix, AND all affected test updates together, ending with a fully green suite. Tasks
2–4 then layer ORCID, funding, and relations on top, each keeping the suite green.

---

### Task 1: Model restructure — `Creator` / `FundingRef` + creators→objects (ATOMIC, green at end)

**Files:**

- Modify: `src/data_aggregator_mcp/models.py` (add types + `_orcid`; change `creators`; add `funding`)
- Modify: `src/data_aggregator_mcp/datacite.py:121`, `zenodo.py:53`, `openaire.py:67`, `pubmed.py:48`
- Modify: `src/data_aggregator_mcp/citation.py:48`
- Test: `tests/test_models.py` + update assertions in `tests/test_datacite.py`,
  `test_zenodo.py`, `test_openaire.py`, `test_pubmed.py`, `test_citation.py`, and any
  `test_router.py`/`test_server.py` test asserting `creators` as strings.

- [ ] **Step 1: Write the failing model test** (`tests/test_models.py`)

```python
from data_aggregator_mcp.models import Creator, FundingRef, DataResource, compact, _orcid


def test_creator_and_funding_types():
    c = Creator(name="Ada Lovelace", orcid="0000-0002-1825-0097")
    assert c.name == "Ada Lovelace" and c.orcid == "0000-0002-1825-0097"
    assert Creator(name="No ORCID").orcid is None
    f = FundingRef(funder="NSF", award="ABC-123")
    assert f.funder == "NSF" and f.award == "ABC-123"


def test_orcid_strips_url_and_validates():
    assert _orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert _orcid("0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert _orcid("0000-0002-1825-009X") == "0000-0002-1825-009X"  # checksum X allowed
    assert _orcid("garbage") is None
    assert _orcid(None) is None


def test_compact_preserves_creators_funding_links():
    r = DataResource(
        id="zenodo:1", source="zenodo", kind="dataset", title="t",
        creators=[Creator(name="A", orcid="0000-0002-1825-0097")],
        funding=[FundingRef(funder="NSF", award="X")],
        description="d" * 1000,
        files=[],
    )
    c = compact(r)
    assert c.creators == r.creators
    assert c.funding == r.funding
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_models.py -k "creator or orcid or compact_preserves" -v` → FAIL (imports missing).

- [ ] **Step 3: Implement the model**

In `models.py` add (near `Link`):

```python
import re

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _orcid(value: str | None) -> str | None:
    """Normalize an ORCID to its bare iD form, or None if absent/malformed."""
    if not value:
        return None
    bare = value.rsplit("/", 1)[-1].strip()
    return bare if _ORCID_RE.match(bare) else None


class Creator(BaseModel):
    name: str
    orcid: str | None = None


class FundingRef(BaseModel):
    funder: str
    award: str | None = None
```

Change `DataResource.creators` to `list[Creator] = Field(default_factory=list)` and add
`funding: list[FundingRef] = Field(default_factory=list)`. Read `compact()` (`models.py:76`) and
confirm it copies `creators`/`funding` through unchanged (it operates by `model_copy` /
field-subset — leave creators & funding intact; only `files`/`description` are touched).

- [ ] **Step 4: Update the 4 adapters (name only — NO ORCID yet) and citation**

- `datacite.py:121`: `creators=[Creator(name=c.get("name", "")) for c in (a.get("creators") or [])]`
- `zenodo.py:53`: `creators=[Creator(name=c.get("name", "")) for c in meta.get("creators", []) or []]`
- `openaire.py:67`: `creators=[Creator(name=a["fullName"]) for a in (record.get("authors") or []) if a.get("fullName")]`
- `pubmed.py:48`: `creators=[Creator(name=a["name"]) for a in doc.get("authors", []) if a.get("name")]`
- `citation.py:48`: `item["author"] = [{"literal": c.name} for c in r.creators]`
- Add `from .models import Creator` (or the right import path) to each adapter.

- [ ] **Step 5: Update existing tests** asserting `creators` as strings → `Creator` objects.

Find them: `grep -rn "creators" tests/`. Update each assertion, e.g. `assert r.creators == ["A"]`
→ `assert r.creators == [Creator(name="A")]` (or assert `r.creators[0].name == "A"`). Update
`test_citation.py` fixtures to pass `Creator` objects. Do NOT weaken assertions — translate them.

- [ ] **Step 6: Run the full suite** — `python -m pytest -q` → ALL pass. Fix every breakage from
      the type change. (This is the atomic-change checkpoint.)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(models): structured Creator objects + funding field (breaking: creators str→obj)"
```

---

### Task 2: DataCite + Zenodo creator ORCID extraction

**Files:**

- Modify: `src/data_aggregator_mcp/datacite.py` (`_normalize` creators), `zenodo.py` (`_normalize` creators)
- Test: `tests/test_datacite.py`, `tests/test_zenodo.py`

- [ ] **Step 1: Write failing tests**

```python
# test_datacite.py
def test_normalize_extracts_creator_orcid():
    item = {"attributes": {"doi": "10.x/y", "titles": [{"title": "t"}], "types": {},
            "creators": [
                {"name": "A", "nameIdentifiers": [
                    {"nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
                     "nameIdentifierScheme": "ORCID"}]},
                {"name": "B"}]}}
    r = datacite._normalize(item)
    assert r.creators[0].orcid == "0000-0002-1825-0097"
    assert r.creators[1].orcid is None

# test_zenodo.py
def test_normalize_extracts_creator_orcid():
    rec = {"id": 1, "metadata": {"title": "t", "creators": [
        {"name": "A", "orcid": "0000-0002-1825-0097"}, {"name": "B"}]}}
    r = zenodo._normalize(rec)
    assert r.creators[0].orcid == "0000-0002-1825-0097"
    assert r.creators[1].orcid is None
```

- [ ] **Step 2: Run to verify fail** — `python -m pytest tests/test_datacite.py tests/test_zenodo.py -k orcid -v` → FAIL.

- [ ] **Step 3: Implement**

DataCite creators (use `_orcid` from models):

```python
def _creator(c: dict) -> Creator:
    orcid = None
    for nid in c.get("nameIdentifiers") or []:
        cand = _orcid(nid.get("nameIdentifier"))
        if cand and (nid.get("nameIdentifierScheme") == "ORCID" or cand):
            orcid = cand
            break
    return Creator(name=c.get("name", ""), orcid=orcid)
# creators=[_creator(c) for c in (a.get("creators") or [])]
```

Zenodo: `creators=[Creator(name=c.get("name",""), orcid=_orcid(c.get("orcid"))) for c in ...]`.
Import `_orcid` (and `Creator`) from `.models`.

- [ ] **Step 4: Run** — `python -m pytest tests/test_datacite.py tests/test_zenodo.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat(datacite,zenodo): extract creator ORCID iDs`.

---

### Task 3: DataCite + Zenodo funding extraction

**Files:** `datacite.py` (`_normalize`), `zenodo.py` (`_normalize`); tests in both.

- [ ] **Step 1: Write failing tests**

```python
# test_datacite.py
def test_normalize_extracts_funding():
    item = {"attributes": {"doi": "10.x/y", "titles": [{"title": "t"}], "types": {},
            "fundingReferences": [
                {"funderName": "NSF", "awardNumber": "ABC-123"},
                {"funderName": "NIH", "awardTitle": "Big Grant"},
                {"awardNumber": "no-funder"}]}}
    r = datacite._normalize(item)
    assert r.funding == [FundingRef(funder="NSF", award="ABC-123"),
                         FundingRef(funder="NIH", award="Big Grant")]

# test_zenodo.py
def test_normalize_extracts_funding():
    rec = {"id": 1, "metadata": {"title": "t",
           "grants": [{"code": "654321", "funder": {"name": "European Commission"}},
                      {"title": "T", "funder": {"name": "NSF"}},
                      {"code": "x"}]}}  # no funder.name → skipped
    r = zenodo._normalize(rec)
    assert r.funding == [FundingRef(funder="European Commission", award="654321"),
                         FundingRef(funder="NSF", award="T")]
```

- [ ] **Step 2: Run to verify fail** — `-k funding` → FAIL.

- [ ] **Step 3: Implement**

DataCite:

```python
funding=[
    FundingRef(funder=f["funderName"], award=f.get("awardNumber") or f.get("awardTitle"))
    for f in (a.get("fundingReferences") or []) if f.get("funderName")
],
```

Zenodo:

```python
funding=[
    FundingRef(funder=(g.get("funder") or {}).get("name"),
               award=g.get("code") or g.get("title"))
    for g in (meta.get("grants") or []) if (g.get("funder") or {}).get("name")
],
```

Import `FundingRef` from `.models`.

- [ ] **Step 4: Run** the two files → PASS.
- [ ] **Step 5: Commit** — `feat(datacite,zenodo): extract funding references`.

---

### Task 4: Related-identifier links (D1)

**Files:** `models.py` (add `_rel` helper) OR a small util; `datacite.py`, `zenodo.py` (`_normalize`); tests in both.

- [ ] **Step 1: Write failing tests**

```python
# test_datacite.py
def test_normalize_extracts_related_links():
    item = {"attributes": {"doi": "10.x/y", "titles": [{"title": "t"}], "types": {},
            "relatedIdentifiers": [
                {"relatedIdentifier": "10.5281/zenodo.1", "relationType": "IsSupplementTo"},
                {"relatedIdentifier": "10.1/v1", "relationType": "IsVersionOf"}]}}
    r = datacite._normalize(item)
    rels = {(l.rel, l.target_id) for l in r.links}
    assert ("is_supplement_to", "10.5281/zenodo.1") in rels
    assert ("is_version_of", "10.1/v1") in rels

# test_zenodo.py
def test_normalize_extracts_related_links():
    rec = {"id": 1, "metadata": {"title": "t", "related_identifiers": [
        {"identifier": "10.1/x", "relation": "isPartOf"}]}}
    r = zenodo._normalize(rec)
    assert ("is_part_of", "10.1/x") in {(l.rel, l.target_id) for l in r.links}
```

- [ ] **Step 2: Run to verify fail** — `-k related_links` → FAIL.

- [ ] **Step 3: Implement**

Add a snake-caser (verify output on `IsSupplementTo`→`is_supplement_to`, `isPartOf`→`is_part_of`):

```python
def _rel(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
```

Note `isPartOf` has a lowercase lead, so this yields `is_part_of` — confirm with the test. Put
`_rel` where both adapters can import it (e.g. `models.py` or `_merge.py`).

In each `_normalize`, build the related links and append to whatever `links` the resource gets.
Since both `_normalize` currently construct `DataResource(...)` without `links`, add a
`links=[...]` kwarg:

- DataCite: `links=[Link(rel=_rel(r["relationType"]), target_id=r["relatedIdentifier"]) for r in (a.get("relatedIdentifiers") or []) if r.get("relationType") and r.get("relatedIdentifier")]`
- Zenodo: `links=[Link(rel=_rel(r["relation"]), target_id=r["identifier"]) for r in (meta.get("related_identifiers") or []) if r.get("relation") and r.get("identifier")]`
  Import `Link` (already imported in zenodo for FileEntry? check) + `_rel`.

- [ ] **Step 4: Run** the two files + full suite → PASS (confirm router `described_in` enrichment
      still appends correctly on top of these base links).
- [ ] **Step 5: Commit** — `feat(datacite,zenodo): expose relatedIdentifiers as links`.

---

### Task 5: Version bump to 0.13.0 + CHANGELOG

**Files:** `pyproject.toml:3`, `__init__.py:3`, `server.json` ×2, `CHANGELOG.md`, `tests/test_packaging.py`.

- [ ] **Step 1:** update `test_packaging.py` asserted version → `0.13.0` (function name + 3 literals).
- [ ] **Step 2:** run `pytest tests/test_packaging.py` → FAIL.
- [ ] **Step 3:** bump all four synced places to `0.13.0`.
- [ ] **Step 4:** prepend CHANGELOG:

```markdown
## [0.13.0] - 2026-05-31

### Changed

- **Breaking:** `creators` is now a list of `{name, orcid}` objects (was a list of
  name strings). ORCID iDs are populated from DataCite `nameIdentifiers` and Zenodo
  creator metadata where available.

### Added

- `funding` — funding references (`{funder, award}`) from DataCite `fundingReferences`
  and Zenodo `grants`.
- Related-identifier `links` — DataCite `relatedIdentifiers` / Zenodo `related_identifiers`
  are surfaced as `links` (verbatim targets; no graph traversal).
```

- [ ] **Step 5:** `pytest -q` → PASS. Commit `chore: bump to 0.13.0 (relations + ORCID + funding)`.

---

### Task 6: Real-execution probe (boundary)

**Files:** `tests/test_router.py` (reuse `_live_only`).

- [ ] **Step 1:** add a `@_live_only` test that resolves a known DataCite DOI carrying
      `relatedIdentifiers` + `fundingReferences` and asserts `links` and `funding` are non-empty,
      and resolves/searches a Zenodo record with a creator ORCID asserting `creators[i].orcid` set.
      (Pick stable DOIs; if unsure, assert the weaker invariant that at least one of links/funding/
      orcid is populated across a small known set.)
- [ ] **Step 2:** run with `DATA_AGGREGATOR_MCP_LIVE=1 python -m pytest tests/test_router.py -k <name> -v` → PASS against real APIs.
- [ ] **Step 3:** commit `test: live probe for relations + ORCID + funding`.

---

## Final review

After all tasks: `pytest -q` green, dispatch a whole-branch code review, then
`superpowers:finishing-a-development-branch` to merge and ship v0.13.0.
