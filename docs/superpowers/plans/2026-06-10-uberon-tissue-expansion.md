# UBERON tissue/anatomy query-expansion (recall, sibling of MeSH disease)

**Status:** PLANNED 2026-06-10. Target v0.27.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** extend the ontology-grounded recall pattern (`organism=` NCBI-Taxonomy, `disease=` MeSH) to a third axis —
`tissue=` via **UBERON** through the **EBI OLS4** API. Especially additive for the single-cell sources
(CELLxGENE/DANDI). This is the FIRST expansion backed by a non-NCBI client, so the OLS contract is grounded live below.

## What it adds

A new opt-in `tissue=` search param. Resolves a tissue/anatomy name through UBERON → canonical label + exact synonyms
→ AND a quoted OR-group into the query (via the shared `router._or_group`). Composes with `organism=` AND `disease=`
(three stacked AND-groups, each opt-in). Echoed in `tissue_expansion`. Mirrors `_expand_disease`/`mesh.py` shape.

## Live-probed OLS4 contract (2026-06-10 — re-probe in the gated live test, do NOT recall)

`GET https://www.ebi.ac.uk/ols4/api/search` with params
`q=<name>&ontology=uberon&exact=true&fieldList=obo_id,label,synonym,is_defining_ontology,is_obsolete&rows=10`.
Response envelope: `body["response"]["docs"]` (list); `body["response"]["numFound"]` (int).
Each doc may carry: `obo_id` (e.g. `"UBERON:0002107"`), `label` (canonical, e.g. `"liver"`), `synonym` (list — MAY be
absent), `is_defining_ontology` (bool — MAY be absent), `is_obsolete` (bool/None — MAY be absent).

**TWO hard filters are load-bearing (both proven necessary by live probe — neither param self-enforces):**

1. **`obo_id` must start with `"UBERON:"`.** `ontology=uberon` does NOT hard-restrict: `q=hepar` leaked `PR:` (Protein
   Ontology) terms into the results. Without this guard the matcher could return a non-UBERON id.
2. **Exact label/synonym match required client-side.** `exact=true` does NOT hard-filter: `q=liver` returns
   `numFound:66` incl. "caudate lobe of liver". The TOP relevance hit is NOT guaranteed canonical — match explicitly.

### Matcher `_pick_uberon(docs, key) -> UberonInfo | None`

`key = name.strip().lower()`. Among `docs`, keep candidates where:
`obo_id` is a str starting `"UBERON:"` AND `is_obsolete` is not truthy AND
(`label.lower() == key` OR `key in {s.lower() for s in (synonym or []) if isinstance(s, str)}`).
Among candidates prefer `is_defining_ontology is True` (tie-break; else first). Return
`UberonInfo(uberon_id=obo_id, canonical=label, synonyms=tuple(s for s in (synonym or []) if isinstance(s,str) and s.strip()))`.
No candidate → None (conservative: an ambiguous/synonym-typo input yields NO expansion, never a wrong term).

**LIVE-TEST OBLIGATION (real-execution boundary):** `resolve_uberon(client, "liver")` → `uberon_id=="UBERON:0002107"`,
`canonical=="liver"`, synonyms include `"iecur"`/`"jecur"`. Also assert `resolve_uberon(client, "notatissuexyz")` is None
(numFound 0). If OLS surface forms drift, fix the ASSERTION, not the code (expectation drift, per the v0.25.0 PDB-casing
and v0.26.0 MeSH catches).

## Build (TDD — test first per unit)

### 1. `src/data_aggregator_mcp/anatomy.py` (NEW — mirror `mesh.py`, but EBI OLS via `_http`, not `_eutils`)

```python
@dataclass(frozen=True)
class UberonInfo:
    uberon_id: str            # "UBERON:0002107"
    canonical: str            # OLS label
    synonyms: tuple[str, ...]

OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
_HEADERS = {"User-Agent": "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"}
_NEG = object()
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)

async def resolve_uberon(client, name) -> UberonInfo | None:
    key = name.strip().lower()
    if not key: return None
    cached = _CACHE.get(key)
    if cached is not MISS: return None if cached is _NEG else cached
    body = await _http.request_json(
        client, "GET", OLS_SEARCH, service="EBI OLS (UBERON)", headers=_HEADERS,
        params={"q": name, "ontology": "uberon", "exact": "true",
                "fieldList": "obo_id,label,synonym,is_defining_ontology,is_obsolete", "rows": "10"},
        timeout=30.0, max_retries=2)
    docs = ((body or {}).get("response") or {}).get("docs") or []
    info = _pick_uberon(docs, key)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
```

- HTTP failures **propagate** (NOT cached) — identical to `taxonomy`/`mesh` (caller surfaces in `errors`).
- `_pick_uberon` is the pure, unit-testable matcher from the contract above. Guard `body`/`response`/`docs` non-dict/non-list.

### 2. `src/data_aggregator_mcp/models.py`

```python
class TissueExpansion(BaseModel):
    """Echo of UBERON tissue-synonym expansion that fired for a search (transparency)."""
    input: str
    uberon_id: str        # "UBERON:0002107"
    canonical_name: str
    synonyms: list[str]   # entry synonyms added (excludes the canonical label)
```

Add to `SearchResult` (after `mesh_expansion`): `tissue_expansion: TissueExpansion | None = None`.

### 3. `src/data_aggregator_mcp/router.py`

- New `_expand_tissue(client, query, tissue, errors) -> tuple[str, TissueExpansion | None]` — verbatim shape of
  `_expand_disease`: blank → `(query, None)`; lookup EXCEPTION → `errors["uberon"] = ...` and `(query, None)`
  (fail-loud); no match → `(query, None)`; on hit, `terms = list(dict.fromkeys([info.canonical, *info.synonyms]))`,
  `or_group = _or_group(terms)`, `effective = f"({query}) AND ({or_group})"`, echo
  `TissueExpansion(input=tissue, uberon_id=info.uberon_id, canonical_name=info.canonical, synonyms=list(info.synonyms))`.
- `search_page` signature: add `tissue: str | None = None` (after `disease`).
- **Fresh-search branch** — compose AFTER the disease line (so org→disease→tissue stack):
  `effective_query, tissue_expansion = await _expand_tissue(client, effective_query, tissue, errors)`.
- **Continuation branch**: `tissue = st.get("tissue")`; `tissue_expansion = None` (frozen — do not re-expand).
- **Cursor echo**: add `"tissue": tissue` to the encoded dict.
- **Return**: add `tissue_expansion=tissue_expansion`.
- Legacy `search()` 4-tuple entrypoint: UNCHANGED.

### 4. `src/data_aggregator_mcp/server.py`

- search `inputSchema.properties` (after `disease`): add
  ```python
  "tissue": {"type": "string", "description": "Optional tissue/anatomy name. Resolved via UBERON (EBI OLS); "
      "the query is expanded with the canonical term + exact synonyms (e.g. 'liver' also matches 'iecur'/'jecur'). "
      "The expansion is echoed in tissue_expansion."},
  ```
- search description blurb: append a clause noting `tissue=<name>` expands via UBERON synonyms, echoed in `tissue_expansion`.
- search handler: add `tissue=args.get("tissue"),` to the `search_page(...)` call. `result.model_dump()` already
  serializes the new field — no other handler change.

### 5. `tests/`

- `tests/test_anatomy.py` (httpx_mock / pytest_httpx like `test_mesh.py`): `_pick_uberon` unit table — exact-label hit;
  synonym-exact hit; **non-UBERON obo_id (PR:…) rejected even if label matches** (the cross-ontology guard);
  obsolete rejected; no-exact-match → None; absent `synonym`/`is_defining_ontology` fields tolerated; defining-ontology
  tie-break. `resolve_uberon`: happy path (OLS envelope → UberonInfo); numFound-0 → None + cached; blank → None no HTTP;
  cache positive+negative single round-trip; HTTP error propagates (not cached). Gated live test per the contract.
- `tests/test_router.py`: `_expand_tissue` builds `(q) AND ("canonical" OR "syn")`; **org+disease+tissue compose into
  three stacked AND-groups**; cursor round-trips `tissue` and does NOT re-expand; `tissue_expansion` echoed; an OLS
  failure lands in `errors["uberon"]` and does NOT raise.
- `tests/test_server.py`: `tissue` in the search inputSchema; handler forwards it; `model_dump()` carries `tissue_expansion`.

## Gates & release

- ruff check + ruff format --check + mypy src (no `# type: ignore`) + `pytest --cov-fail-under=92`.
- Bump `pyproject.toml` + `__init__.__version__` + `server.json` → `0.27.0`; update `test_packaging.py`; CHANGELOG entry.
- **Release ONLY after the user confirms.** Flow: ff-merge → push → trailer-free tag `v0.27.0` → `gh release create`.

## Spec contracts the reviewers must enforce

- **Both hard filters (`UBERON:` prefix + exact label/synonym) are load-bearing** — neither OLS param self-enforces.
- **Conservative no-match → None** (no expansion), but a real OLS HTTP failure is **fail-loud** `errors["uberon"]`
  (search-input expansion, same as taxonomy/mesh — NOT a fail-soft resolve enricher).
- **Compose order:** tissue expands the org+disease-expanded query; three opt-in AND-groups can stack.
- **Continuation never re-expands** (page consistency): `tissue_expansion=None`, `tissue` read from cursor.
- New external client → set a descriptive `User-Agent`; reuse `_http.request_json` (retries/timeouts) + shared `_or_group`.
