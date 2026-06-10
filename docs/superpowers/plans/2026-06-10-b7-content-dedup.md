# B7 — Cross-repo content dedup beyond DOI (opt-in mirror collapse)

**Status:** PLANNED 2026-06-10. Target v0.31.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `collapse_mirrors=true` search parameter that collapses records which are the SAME dataset
deposited under different (or no) DOIs — e.g. a Zenodo mirror of a figshare deposit, GEO↔ArrayExpress — into one
record, annotating the survivor with the collapsed copies under a new `mirrors[]` field. From the v4 memo
[[data-aggregator-competitive-analysis-v4-2026-06-10]] (B7, Frontier #6) — THE aggregator-native moat extension: only
possible because we fan out across 12 sources; a single-source tool structurally cannot see a cross-repo mirror.

## Why opt-in + conservative (the load-bearing safety decision)

Exact-DOI dedup (`router._dedup`) is safe because a shared DOI is a definitional identity. Content dedup is a
HEURISTIC — a false merge silently hides a genuinely distinct dataset, which is worse than a missed merge. Therefore:

- **Opt-in** (`collapse_mirrors`, default `False`). DOI dedup stays always-on; content collapse is a deliberate choice.
- **Conservative fingerprint, HIGH-confidence only** (no fuzzy/shingle matching in v1 — deferred):
  - **Identity by shared file checksum** — if two records share ANY `files[].checksum` (`"<algo>:<hex>"`, compared on
    the full `algo:hex`), they are byte-identical data → merge. Strongest signal.
  - **Identity by (normalized-title, first-author-surname, year)** — merge only when ALL THREE are present, non-empty,
    and equal after normalization, **AND the two records come from DIFFERENT sources**. A title match alone, or a
    missing year on either side, is NOT enough. The cross-source requirement is load-bearing: B7 is _cross-repo_ dedup —
    two SAME-source records sharing title+author+year are almost always **version siblings** (e.g. Zenodo record v1/v2),
    a relationship already modeled by B1 (`is_latest`/`superseded_by`); folding them as "mirrors" would be wrong. Only a
    copy in a DIFFERENT repository is a mirror. (Confirmed on live data: the dominant real fingerprint collision is
    consecutive same-source Zenodo deposits — correctly NOT folded.) The checksum path stays source-agnostic.
  - `_normalize_title`: lowercase, strip surrounding/duplicate whitespace, drop punctuation → compare for EXACT
    normalized equality (not substring, not fuzzy). `_first_author_surname`: last whitespace-token of `creators[0].name`
    lowercased (best-effort; if no creators, the title+author+year path cannot fire).
- **Annotate, never silently drop.** The survivor gains `mirrors: list[Mirror]` where each `Mirror` records the
  collapsed copy's `source`, `id`, `doi`. The user always sees that a collapse happened and what was folded in.
- **Survivor selection** (deterministic): prefer a DOI-bearing record over a DOI-less one; among DOI-bearing, prefer a
  natively-fetchable id (not `datacite:`-prefixed) — same precedence spirit as `_dedup`. Ties broken by first-seen order.

## Where it runs (pagination-safe integration)

In `router.search_page`, collapse runs on the **`emitted`** list (the records actually returned for this page) AFTER
`_enrich` + `_with_version_status` (so the fingerprint sees any enrich-added files/checksums), immediately before the
`SearchResult` is built. The `consumed` / `cut` / per-adapter offset accounting and `next_cursor` are computed from
`consumed` and are NOT touched — collapse is a presentation-layer fold over `emitted`, so it cannot corrupt offsets or
stall pagination.

- **Honest scope = intra-page, best-effort.** A mirror that lands on a different page (or past the `size` cut) is NOT
  collapsed — a stateless server holds no cross-page index. Document this plainly. A page may therefore return FEWER
  than `size` items when mirrors are folded (acceptable; the `more`/cursor logic is unaffected).
- The `collapse_mirrors` flag is a new `search_page` kwarg, threaded into the cursor dict (encode at ~L447 + decode at
  ~L335) so continuation pages keep collapsing consistently — exactly like `disease`/`tissue`/`rank`.

## Models (models.py)

- New `Mirror(BaseModel)`: `source: str`, `id: str`, `doi: str | None = None`. Docstring: "a same-dataset copy folded
  into this record by content dedup (resolve the mirror's id to reach the original deposit)."
- `DataResource.mirrors: list[Mirror] = Field(default_factory=list)` (after `access_modes`). Empty by default — only
  populated when `collapse_mirrors` folded copies in. Inline comment noting it's opt-in via search `collapse_mirrors`.

## Router (router.py)

- `_normalize_title(title: str) -> str` and `_first_author_surname(r) -> str | None` helpers (module-level, pure).
- `_fingerprint_key(r) -> tuple | None` → the `(norm_title, surname, year)` key when all three present, else None.
- `_collapse_mirrors(records: list[DataResource]) -> list[DataResource]`: a single pass that groups by checksum-identity
  AND fingerprint-key identity (a record matches a group if it shares a checksum with any member OR has the same
  fingerprint key), picks the survivor per the precedence above, sets `survivor.mirrors` (via `model_copy`) to the other
  members' `Mirror(source,id,doi)`, preserves first-seen order of survivors. PURE, deterministic, no I/O. Conservative:
  when in doubt, do NOT merge.
- `search_page`: add `collapse_mirrors: bool = False`; read from kwarg (fresh) or cursor (continuation); after
  `enriched = [_with_version_status(r) for r in enriched]`, do `if collapse_mirrors: enriched = _collapse_mirrors(enriched)`.
  Add `collapse_mirrors` to the cursor encode dict.

## Server (server.py)

- Add a `collapse_mirrors` boolean to the `search` inputSchema (and `search_resolve_fetch` if it shares the search
  schema — check live), documenting: opt-in cross-repo mirror collapse, intra-page/best-effort, survivor annotated with
  `mirrors[]`, conservative (checksum OR title+author+year). Pass `collapse_mirrors=args.get("collapse_mirrors", False)`
  into the `search_page` call(s). Extend the search tool description with one sentence.

## Tests (tests/test_content_dedup.py + wiring)

- `_normalize_title` / `_first_author_surname`: table of forms (punctuation, case, whitespace, single-name authors).
- `_collapse_mirrors` unit:
  - two records, same normalized title + same first-author surname + same year, different DOIs → collapsed to ONE;
    survivor carries one `Mirror` for the folded record.
  - shared file checksum, DIFFERENT titles → still merged (checksum is definitional identity).
  - same title + author but DIFFERENT year → NOT merged (conservative).
  - same title, year, but NO creators on either → NOT merged via the title path (surname unavailable); merged only if a
    checksum matches.
  - survivor selection: DOI-bearing beats DOI-less; native id beats `datacite:`; first-seen tiebreak. Assert which id wins.
  - three-way mirror group → one survivor, two `Mirror`s.
  - distinct datasets (different title+author) → untouched, no `mirrors`.
  - PURE/deterministic: same input twice → identical `model_dump()`; no httpx in the module path used.
- search wiring:
  - `collapse_mirrors` in the `search` inputSchema (boolean, not required); default off → `mirrors` empty / no collapse.
  - a `search_page` run (mocked adapters returning two mirror records on one page) with `collapse_mirrors=True` collapses
    them and annotates; with the flag off, both records are returned separately. (Use the existing search-test harness
    /fixtures — read how other router tests mock adapters.)
  - the cursor round-trips `collapse_mirrors` (a continuation page keeps collapsing).
- `DataResource.mirrors` defaults to `[]`; `model_dump()` carries it.
- **Real-execution check (gated `DATA_AGGREGATOR_MCP_LIVE=1`):** run a REAL `search_page(..., collapse_mirrors=True)`
  for a query likely to surface a Zenodo/figshare or GEO/ArrayExpress mirror; assert it does not crash, every emitted
  record's `mirrors` (if any) have plausible distinct ids/sources, and NO record lists itself as its own mirror. If no
  mirror appears in the live page that's acceptable (assert the no-crash + well-formedness invariants), and also do one
  positive check by resolving two KNOWN cross-repo copies and asserting `_fingerprint_key` / checksum identity matches —
  so the live test proves the fingerprint fires on real metadata, not just synthetic fixtures.

## Explicitly OUT of scope (deferred — keep the wave tight + safe)

- **Fuzzy / shingle / near-duplicate title matching** — false-merge risk; v1 is exact-normalized-title + author + year,
  or shared checksum. Note as a deferred follow-up in CHANGELOG.
- **Cross-page mirror collapse** — needs state the stateless server doesn't hold. Intra-page only; documented.
- **Default-on collapse** — stays opt-in until the fingerprint has proven low-false-merge in the field.

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.31.0** (pyproject/**init**/server.json×2/
  test_packaging) + CHANGELOG. Release after green (standing program authorization). ff-merge → tag v0.31.0.

## Spec contracts reviewers must enforce

- **Conservative: no false merge.** A merge requires shared checksum OR (normalized-title AND first-author-surname AND
  year) all present and equal **AND different sources**. Title-alone or partial matches must NOT merge.
- **Cross-source fingerprint** — the title+author+year path folds only records from DIFFERENT sources; same-source
  matches are version siblings (B1's domain), never mirrors. (Checksum path is source-agnostic.)
- **Never silently drop** — every folded copy appears in the survivor's `mirrors[]` with source+id(+doi). No record is
  its own mirror.
- **Opt-in** — default `collapse_mirrors=False`; DOI dedup unchanged; with the flag off the search output is byte-identical
  to pre-B7.
- **Pagination untouched** — `consumed`/offset/`next_cursor` computed before collapse; cursor round-trips the flag.
- **PURE collapse** — `_collapse_mirrors` is deterministic, no I/O.
- Survivor selection is deterministic and documented (DOI-bearing > DOI-less; native > datacite; first-seen tiebreak).
