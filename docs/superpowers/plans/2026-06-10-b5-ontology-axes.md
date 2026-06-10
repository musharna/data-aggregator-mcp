# B5 — Ontology recall axes 4 & 5: `chemical=` (ChEBI) + `assay=` (EDAM)

**Status:** PLANNED 2026-06-10. Target v0.32.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** add two more ontology-grounded search-input expansion axes, cloning the PROVEN
organism/disease/tissue `_pick`/`resolve`/`_expand` shape — only the ontology id + obo_id prefix change. From the v4 memo
[[data-aggregator-competitive-analysis-v4-2026-06-10]] (B5, Frontier #9). Compounds the recall moat: a query for
`chemical=caffeine` ANDs in ChEBI synonyms (1,3,7-trimethylxanthine, …); `assay=ChIP-seq` ANDs in EDAM synonyms
(ChIP-exo, ChIP-sequencing, …). Single-source tools can't reconcile these.

## Grounding (LIVE OLS4 probe 2026-06-10 — both behave like UBERON)

- **ChEBI** (`ontology=chebi`): `q=glucose&exact=true` → `CHEBI:17234` label `glucose`; `q=caffeine` → `CHEBI:27732`.
  Like UBERON, the top relevance hit under `exact=true` is NOT guaranteed canonical (`q=aspirin` surfaces
  "aspirin-triggered protectin D1" before the real `aspirin`/`CHEBI:15365`) — the exact label/synonym filter is
  load-bearing exactly as in `anatomy._pick_uberon`. obo_id prefix filter: `"CHEBI:"`.
  - **ChEBI-specific: CAP synonyms.** ChEBI synonym lists are large (many IUPAC variants). Cap the synonyms ANDed into
    the query to a bounded number (`_MAX_SYNONYMS = 12`, canonical always kept) so the OR-group stays a sane query size.
    UBERON/MeSH need no cap (few exact synonyms); this is a ChEBI realism, documented.
- **EDAM** (`ontology=edam`) is the right backend for the assay/method axis (NOT OBI): `q=ChIP-seq&exact=true` →
  `EDAM:topic_3169` label `ChIP-seq` with rich synonyms `[ChIP-exo, ChIP-sequencing, Chip Seq, Chip-sequencing]`;
  `q=RNA-Seq` → `EDAM:topic_3170`. OBI returns the term but `synonym=[]` (zero recall value — the whole point of the
  axis is synonym expansion). EDAM mixes id-classes (`topic_`, `data_`, `format_`, `operation_`); restrict to
  **`obo_id.startswith("EDAM:topic_")`** (assay/method concepts are EDAM topics; `operation_` deferred). Exact
  label/synonym match + scalar-synonym normalization apply exactly as UBERON.

## Design — two new helper modules cloning `anatomy.py` (NOT router adapters)

- `src/data_aggregator_mcp/chemistry.py`: `ChebiInfo(chebi_id, canonical, synonyms)`, `_pick_chebi(docs, key)`,
  `resolve_chebi(client, name)`. Mirror `anatomy.py` verbatim EXCEPT: obo_id prefix `"CHEBI:"`, `service="EBI OLS (ChEBI)"`,
  `ontology=chebi`, and cap `synonyms` to `_MAX_SYNONYMS = 12` in `_pick_chebi` (keep canonical + first 12 exact synonyms).
  Same in-process `TTLCache` (negative cached), same scalar-synonym normalization, same `is_defining_ontology` tiebreak,
  HTTP failures propagate (not cached).
- `src/data_aggregator_mcp/assay.py`: `EdamInfo(edam_id, canonical, synonyms)`, `_pick_edam(docs, key)`,
  `resolve_edam(client, name)`. Mirror `anatomy.py` EXCEPT: obo*id prefix `"EDAM:topic*"`, `service="EBI OLS (EDAM)"`,
`ontology=edam`. No synonym cap needed (EDAM lists are small).
- Reuse the shared `_http.request_json`, `_cache.TTLCache`/`MISS`, and the `_HEADERS` user-agent pattern from anatomy.

## Models (models.py) — clone `TissueExpansion`

- `ChemicalExpansion(BaseModel)`: `input: str`, `chebi_id: str`, `canonical_name: str`, `synonyms: list[str]`.
- `AssayExpansion(BaseModel)`: `input: str`, `edam_id: str`, `canonical_name: str`, `synonyms: list[str]`.
- `SearchResult`: add `chemical_expansion: ChemicalExpansion | None = None` and
  `assay_expansion: AssayExpansion | None = None` (after `tissue_expansion`).

## Router (router.py) — clone `_expand_tissue`, chain after tissue

- `_expand_chemical(client, query, chemical, errors) -> (str, ChemicalExpansion | None)` and
  `_expand_assay(client, query, assay, errors) -> (str, AssayExpansion | None)` — byte-for-byte the `_expand_tissue`
  shape: empty/blank → `(query, None)`; resolver exception → record `errors["chebi"]` / `errors["edam"]` and return
  un-expanded (FAIL-LOUD — search-input expansion, not a fail-soft resolve enricher); no-match → un-expanded, no error;
  match → `terms = dict.fromkeys([canonical, *synonyms])`, `_or_group(terms)`, `f"({query}) AND ({or_group})"` + echo.
- `search_page`: add `chemical: str | None = None`, `assay: str | None = None` kwargs. In FRESH mode, after the
  `_expand_tissue` call (~L488), chain:
  ```python
  effective_query, chemical_expansion = await _expand_chemical(client, effective_query, chemical, errors)
  effective_query, assay_expansion = await _expand_assay(client, effective_query, assay, errors)
  ```
  In CONTINUATION mode (~L468), read `chemical = st.get("chemical")`, `assay = st.get("assay")`, and set
  `chemical_expansion = assay_expansion = None` (frozen — do not re-expand, exactly like tissue). Add `"chemical"` and
  `"assay"` to the cursor ENCODE dict (~L575). Pass `chemical_expansion`/`assay_expansion` into the `SearchResult(...)`.

## Server (server.py) — clone the `tissue` wiring

- Add `chemical` and `assay` string properties to the `search` inputSchema (next to `tissue`), documenting: name of a
  chemical/compound (ChEBI) / an assay or method (EDAM topic); resolves to canonical + exact synonyms ANDed into the
  query; unknown term → no expansion; an OLS failure surfaces in `errors`. Extend the search description with one phrase.
- In the `search` handler, pass `chemical=args.get("chemical")`, `assay=args.get("assay")` into `router.search_page(...)`.

## Tests (tests/test_chemistry.py, tests/test_assay.py, + router/search wiring)

- `_pick_chebi` / `_pick_edam` PURE matcher tests (mirror `tests/test_anatomy.py` — READ it first for the fixture
  shape): exact label match; exact synonym match; wrong-prefix doc rejected (ChEBI: reject a non-`CHEBI:` obo*id leak;
  EDAM: reject `EDAM:data*`/`EDAM:format*`, accept only `EDAM:topic*`); obsolete rejected; scalar-synonym normalization
(single string synonym not exploded into chars); no exact match → None (conservative); `is_defining_ontology` tiebreak.
- **ChEBI synonym cap**: a doc with >12 synonyms → `_pick_chebi` keeps canonical + exactly 12; ≤12 unaffected.
- `_expand_chemical` / `_expand_assay` (mock the resolver): blank → no expansion; no-match → un-expanded + no error;
  resolver raises → `errors["chebi"]`/`errors["edam"]` set + un-expanded (FAIL-LOUD); match → query ANDs the OR-group and
  the echo carries id/canonical/synonyms. (Mirror the existing `_expand_tissue` tests — grep for them.)
- search wiring: `chemical`/`assay` in the `search` inputSchema (string, not required); handler threads them; the cursor
  round-trips both (a continuation page does NOT re-expand); `SearchResult.chemical_expansion`/`assay_expansion` serialize.
- model defaults: both expansions default None; `model_dump()` carries them.
- **Real-execution check (gated `DATA_AGGREGATOR_MCP_LIVE=1`):** live `resolve_chebi(client, "caffeine")` → `CHEBI:27732`,
  canonical "caffeine", synonyms non-empty and capped ≤12; `resolve_edam(client, "ChIP-seq")` → `EDAM:topic_3169`,
  canonical "ChIP-seq", synonyms include a "ChIP-seq"-variant; a junk term (`resolve_chebi(client, "zzzznotachemical")`)
  → None. Proves the OLS contract + prefix filter on real metadata (a synthetic fixture can't catch an OLS field rename).

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.32.0** (pyproject/**init**/server.json×2/
  test_packaging) + CHANGELOG (house style — document both axes, the EDAM-topic restriction, the ChEBI synonym cap, and
  the fail-loud-on-OLS-error parity with organism/disease/tissue). Release after green. ff-merge → tag v0.32.0.

## Spec contracts reviewers must enforce

- **Conservative: no wrong expansion.** Exact label/synonym match only; no match → no expansion (never guess a term).
- **Prefix filters load-bearing** — ChEBI keeps only `CHEBI:`; EDAM keeps only `EDAM:topic_` (drop data*/format*/
  operation\_). A cross-ontology or wrong-id-class leak must be rejected (OLS `ontology=`/`exact=` do NOT self-enforce).
- **Fail-LOUD on OLS error** — `errors["chebi"]`/`errors["edam"]`, query returned un-expanded; parity with the other
  three axes (this is a search-input expansion, the OPPOSITE of a fail-soft resolve enricher).
- **ChEBI synonyms capped** to bound query size; canonical always retained.
- **Pagination/cursor** round-trips `chemical`+`assay`; continuation does not re-expand (frozen echoes).
- **Pure `_pick_*`**, deterministic; HTTP failures propagate and are NOT cached.
