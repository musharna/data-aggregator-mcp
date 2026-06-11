# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.40.0] - 2026-06-11

### Fixed

- **NCBI rate limit no longer over-claims on `NCBI_EMAIL` alone.** The limiter granted
  10 req/s when either `NCBI_API_KEY` or `NCBI_EMAIL` was set; NCBI only raises the
  ceiling for an API key (email is identification). Email-only configs now correctly
  stay at 3 req/s instead of inviting 429s across omics/literature/taxonomy.
- **Search fan-out survives sub-task cancellation.** Both fan-out paths checked
  `isinstance(outcome, Exception)`, so an `asyncio.CancelledError` (a `BaseException`)
  fell through to an assert and destroyed all sources' results. Both now guard on
  `BaseException`, matching `relate`'s handling — a cancelled adapter degrades to a
  per-source error with partial results returned.
- **Cursor decode validates field types.** A crafted cursor (e.g. `variants` as a
  string) previously fanned out garbage one-character queries; `decode` now rejects
  non-list `variants`, non-dict `offsets`, and non-positive `size` as corrupt.
- **`collapse_mirrors` finds transitive merges.** Greedy single-pass grouping could
  strand a byte-identical copy depending on arrival order (A↔C via md5, B↔C via
  sha — B stranded); groups sharing any checksum or fingerprint key are now unioned
  to a fixpoint. Survivor selection unchanged.
- **DataONE Solr queries escape user input.** A query could restructure the boolean
  and strip the `formatType:METADATA` filter; pids with embedded quotes broke the
  phrase query. Lucene specials are now escaped in `search`, and `resolve` escapes
  pid/resourceMap values.
- **GWAS pagination uses the capped page size.** `size>50` computed the page number
  from the raw size while requesting capped pages, skipping result windows.
- **OSF file listing is page-capped** (10 pages ≈ 500 files), matching the DANDI/
  CELLxGENE manifest-cap pattern, instead of following `links.next` unboundedly.
- **DataCite/Scholix error-taxonomy escapes closed.** A 200 without `data` raised a
  bare `KeyError` from DataCite resolve (now a typed `NotFoundError`); a non-JSON
  Scholix body raised `JSONDecodeError` through the resolve path (links are
  enrichment — now degrades to no links).
- **EuropePMC lookups phrase-quote PMCID/DOI values** (second-order injection guard
  for identifier values arriving from upstream APIs).
- **OpenNeuro snapshot queries use GraphQL variables** instead of f-string
  interpolation into the query text.

### Security

- **Archive extraction streams in 64 KiB chunks** counting actual bytes (not declared
  header sizes) against the ceiling, unlinking partials mid-stream on overrun — and no
  longer loads whole members into RAM.
- **Extraction shares the fetch byte budget.** `extract=true` previously granted the
  full `max_bytes` again after the download consumed it (up to 2× disk write); the
  archive now extracts into the remaining headroom and debits what it writes.
- **URL scheme allowlist on fetch and operate.** `FileEntry.url` values from upstream
  metadata are rejected unless `http(s)`; `operate` additionally allows `file://` only
  with `DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS=1` (test fixtures), closing a poisoned-
  metadata local-file read via the fsspec schema/preview paths.
- **Registry publish workflow hardened**: `actions/checkout` SHA-pinned like every
  other workflow, and the `mcp-publisher` binary install is sha256-verified instead
  of `curl | tar`.

### Changed

- **Tool descriptions now tell the whole truth.** `search`'s `sources` list names all
  12 adapters (dandi/openml/pdb/gwas/cellxgene were hidden); `fetch`'s fetchable list
  covers every wired backend with its verification status; `list_sources` health
  probing is scoped to the 5 actually-probed sources; `multi_query`'s always-on
  semantic re-rank is documented; the GWAS catalog entry states its exact
  disease-trait matching.
- **`server.json` documents the full env surface** (`LLM_API_BASE`/`LLM_API_KEY`/
  `LLM_MODEL`, `EMBEDDING_API_BASE`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL`,
  `UNPAYWALL_EMAIL` — previously only `NCBI_API_KEY`), so registry/deployment tooling
  can surface the knobs behind `understand=`, `multi_query=`, `rank=semantic`, and the
  Unpaywall full-text leg.
- README tool signatures updated to the live parameter surface (search filters,
  resolve `trust`/`fair`/`use`, operate `peek`); `PUBLISH.md` made version-agnostic
  (was frozen at the v0.11.0 instructions); the packaging test asserts version sync
  without a hardcoded literal.
- README visual refresh: the intro and sources table cover the full 12-source
  roster (DANDI, CELLxGENE, OpenML, RCSB PDB, GWAS Catalog, and
  DataCite→OpenNeuro were missing), a new architecture diagram
  (`docs/assets/architecture.svg`), and absolute asset/link URLs so the PyPI
  long description renders the demo and links correctly.

## [0.39.1] - 2026-06-11

### Fixed

- **`relate` now canonicalizes DOI forms (L1).** `relate` matched ids by exact string,
  so the same DOI in bare (`10.x`), `doi:`-scheme, and resolver-URL
  (`https://doi.org/10.x`) form failed to match — `version_lineage`, `explicit_link`,
  and `shared_identifier` silently missed real connections (e.g. a DataCite version
  edge expressed as a resolver URL). The identifier normalizer now strips DOI
  resolver/scheme prefixes, symmetrically across all four detectors.
- **`relate` reports version cycles as contradictions (L2).** A mutual / cyclic
  `superseded_by` (contradictory upstream metadata) previously yielded a single hint
  whose newer/older direction was arbitrary. `version_lineage` now reachability-detects
  cycles of any length and emits a contradiction hint (no asserted direction) for
  cyclic pairs, the normal directional hint otherwise.

### Changed

- Refreshed `examples/assets/demo.svg` to show all six tools (adds `operate`, `relate`)
  and the current source roster.

## [0.39.0] - 2026-06-11

### Added

- **`relate(ids)` — cross-resource join/harmonization hints (B9).** A 6th tool: given
  2–10 resource ids, it resolves each (cached) and emits evidence-backed, metadata-level
  hints — shared accession (BioProject/SRA/GEO), shared cross-identifier (doi/pmid/pmcid),
  explicit link between inputs, and version lineage. HINTS ONLY: no file reads, no column
  comparison, no executed joins. Per-id resolve failures are reported, not fatal.

## [0.38.0] - 2026-06-11

### Fixed

- **`search(understand=true)` no longer regresses recall.** A live recall eval against a
  verified gold set measured a mean recall@20 lift of **−0.40** for `understand=true`: the
  rewriter promoted _every_ LLM-inferred facet (organism/disease/tissue/chemical/assay) into a
  mandatory ANDed clause via the `_expand_*` resolvers, plus a `kind` post-filter — so a
  multi-facet natural-language query collapsed its result set (e.g. 20 → 2) against free-text
  keyword upstreams whose metadata can't satisfy every facet. The cross-domain efficacy of
  query-understanding never transferred to a stateless keyword fan-out.
  - **Fix (mechanism removal):** `understand=true` now _normalizes_ the query rather than
    synthesizing a faceted one. `keyword_core` retains the scientific/entity terms (only
    conversational fluff is stripped), and the entity facets **and `kind`** are **echoed in
    `query_understanding.extracted` for transparency but never auto-applied**. Only
    caller-passed facets drive the `_expand_*` resolvers / `kind` filter; explicit `year`
    scopes are still applied.
  - **Result:** mean recall@20 lift improved from −0.40 to −0.10 (4/5 verified queries now
    neutral-or-positive). The remaining variance is per-query keyword-rewrite term-loss
    (inherent to NL→keyword rewriting), not a structural bug. `understand=true` remains
    opt-in / default-off and is best validated per-deployment with your own LLM;
    `multi_query=true` (recall can only go up) is the safer recall lever.

### Changed

- **Verified recall-eval gold sets.** The `scripts/eval_understand_fixture.json` and
  `scripts/eval_multi_query_fixture.json` anchor sets were rebuilt — the previous fixtures
  paired real-looking DOIs with unrelated or no-longer-resolving records. Each anchor is now
  verified live (resolves _and_ on-topic by title). The eval harness code was unchanged.

## [0.37.0] - 2026-06-10

### Added

- **`search(multi_query=true)` — opt-in diverse multi-query recall expansion (A2.P2).** When
  enabled AND an LLM endpoint is configured, the LLM generates up to a few DELIBERATELY-DIVERSE
  reformulations of the query (different facets/synonyms/framings, not paraphrases), each is
  fanned out across every source, and the deduped union is re-ranked against your ORIGINAL
  query — surfacing relevant records a single keyword query would miss. This is phase 2 of A2
  (the biggest query-side recall lever); it composes with `understand=` (P1 structures one
  query, P2 fans out N variants). P3 (OpenAlex semantic federation) is optional next.
  - **The original query is ALWAYS variant 0.** Recall can only go UP — multi-query adds
    candidates on top of the (post-`understand`, post-ontology-expansion) single-query
    baseline; it never drops below it. Variants are case-insensitively deduped and capped at
    `MAX_QUERY_VARIANTS` (4, incl. the original), bounding the N× upstream cost.
  - **Composite-key window-paginated fan-out.** The single-query offset/cursor model keys by
    `source` and is left BYTE-IDENTICAL; multi-query runs a PARALLEL fan-out keyed by a
    composite `(variant_index, source)` label. Cross-variant duplicates (the same record
    surfaced by two variants) dedup to ONE — recall without duplication. Pagination advances
    per composite key; the cursor stores the EXPANDED variant strings so a continuation
    re-fans the frozen variants with NO LLM call and NO re-expansion.
  - **Re-rank anchored on the ORIGINAL query.** The union has no single coherent upstream
    order, so multi-query always engages the window-rank consumption model and re-ranks the
    whole window against the user's original pre-expansion query via the shipped
    `embeddings.rerank`. No embedding endpoint → interleaved order + an `errors['semantic']`
    note (still a recall win, just unranked — honest).
  - **Opt-in, fail-soft, zero new required deps.** A new `query_understanding.expand` mirrors
    the existing `rewrite`/`embeddings` fail-soft discipline exactly: enabled only by
    `LLM_API_BASE`. With no endpoint configured — or on ANY LLM/parse failure — the search
    degrades to a normal single-query search (variant 0) with a transparency note in
    `errors['multi_query']`; the LLM call can NEVER raise into the search path.
  - **Transparent echo.** A new `SearchResult.query_expansion` echoes the original `input` and
    the raw `variants` actually fanned out (original first); per-variant ontology expansion is
    still shown by the `*_expansion` echoes (the same params apply to every variant).
  - **Byte-identical single-query path.** With the flag off (the default) search is
    byte-identical and no LLM call is attempted; the entire pre-existing search + cursor suite
    passes untouched. Embedding-distance variant diversity is deferred (v1 uses
    prompt-demanded diversity + case-insensitive dedup).
  - **Eval harness shipped.** A gated (`DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE`)
    `scripts/eval_multi_query.py` + labeled JSON fixture runs each query with multi-query off
    vs on and prints per-query + mean recall@20 lift.

## [0.36.0] - 2026-06-10

### Added

- **`search(understand=true)` — opt-in LLM NL→structured-query rewriting (A2.P1).** When
  enabled AND an LLM endpoint is configured, a free-text query is rewritten into a keyword
  core + structured params (organism/disease/tissue/chemical/assay + kind/year) BEFORE the
  existing fan-out runs — raising recall on the QUERY side without an owned corpus or vector
  index. Off by default; this is phase 1 of A2 (P2 diverse multi-query expansion is next).
  - **Propose-validate-dispose guardrail — the LLM proposes, the deterministic resolvers
    dispose.** The rewriter only PROPOSES entities (`organism="Zea mays"`, etc.); the
    already-shipped `_expand_organism`/`_disease`/`_tissue`/`_chemical`/`_assay` resolvers
    (NCBI Taxonomy / MeSH / UBERON / ChEBI / EDAM) and the `kind`/year validators are the
    sole trust surface. A hallucinated entity that doesn't resolve simply yields no
    expansion — exactly like a user typo. No new fabricated taxonomy, no new trust surface.
  - **Opt-in, fail-soft, zero new required deps.** A new `llm.py` mirrors the existing
    `embeddings.py` pattern exactly: an OpenAI-compatible `/chat/completions` endpoint
    enabled only by `LLM_API_BASE` (`LLM_API_KEY` optional for keyless local servers,
    `LLM_MODEL` a passthrough string defaulting to `gpt-4o-mini`). With no endpoint
    configured — or on ANY LLM/parse error — the search runs byte-identically to before (the
    raw keyword query) with a transparency note in `errors['understand']`; the LLM call can
    NEVER raise into the search path.
  - **Explicit caller params always win.** The rewriter only FILLS fields the caller left
    None; if the caller passed `organism=`/`kind=`/a year explicitly, the LLM's value for
    that field is ignored and recorded under `overridden`.
  - **Transparent echo.** A new `SearchResult.query_understanding` echoes the raw `input`,
    the `keyword_core` actually used, the full `extracted` interpretation (every non-null
    field the LLM returned), the `applied` subset, and the `overridden` fields — mirroring
    the `*_expansion` echo honesty. Nothing the LLM did to the query is hidden; ontology
    entities still had to resolve to expand, which the `*_expansion` echoes show.
  - **Pagination stays consistent.** Understanding runs ONCE on the fresh search and mutates
    query/params BEFORE the cursor is encoded, so a paged understood-search replays the
    POST-rewrite query/params page-to-page; the continuation branch never re-understands.
  - **Eval harness shipped.** A gated (`DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE`)
    `scripts/eval_understand.py` + labeled JSON fixture runs each NL query with understand
    off vs on and prints per-query + mean recall@20 lift (a "show it works" instrument, not
    a hard assertion — live recall varies).
  - **Additive.** With the flag off (the default) search is byte-identical and no LLM call is
    attempted; the entire pre-existing search suite passes untouched.

## [0.35.0] - 2026-06-10

### Added

- **`operate(op="peek")` — a pre-download, normalized column profile.** A new mode on the
  existing `operate` tool that profiles every column of a remote tabular file WITHOUT
  downloading it: per-column type, null-rate, approximate distinct count, min/max, and
  numeric quartiles. This turns `operate` into a discovery-time advantage — a gateway that
  only proxies bytes can't answer "what does this file actually contain?" before a fetch.
  - **One DuckDB `SUMMARIZE`, reusing the hardened engine.** `peek` routes through the same
    `duckquery._connect` lockdown path as head/sql — the source is materialized into the
    in-memory `data` table while the local FS is still enabled, then the FS is sealed
    (`disabled_filesystems='LocalFileSystem'`) and config locked, and `SUMMARIZE data` runs
    against the in-memory table AFTER the lock. `peek` takes NO user SQL, so it adds no
    injection surface; the lockdown sequence is untouched.
  - **Honest naming.** The approximate distinct count is surfaced as `approx_unique` (a
    HyperLogLog estimate — never a bare `distinct`/`unique` implying exactness);
    `null_percentage` is a real computed `float` (e.g. one null in three → `33.33`);
    numeric stats (`avg`/`std`/`q25`/`q50`/`q75`) are `None` for text columns rather than a
    fabricated `0`. The per-column SUMMARIZE `count` is OMITTED: it is the TOTAL row count
    (identical for every column), NOT a non-null count, so surfacing it as "count" would
    mislead — the top-level `row_count` plus `null_percentage` give non-null counts honestly.
  - **Normalized across formats.** Parquet and CSV yield the same profile keys, so a caller
    gets one uniform answer regardless of source format.
  - **Size-gated like head/sql.** `SUMMARIZE` scans the whole materialized table, so `peek`
    has the same RAM profile as head/sql and honors `SOURCE_BYTE_CEILING` (100 MB) — an
    oversized source fails loud instead of OOMing. `schema`/`preview` stay ungated.
  - **Additive.** The four existing ops (`schema`/`preview`/`head`/`sql`) are byte-identical;
    `peek` is purely new (one enum value + one engine function).
  - **Deferred (noted for the future):** a Parquet-footer fast path could read null_count/
    min/max from the footer (range-reads only, skipping the full load and the size gate for
    Parquet), but it splits Parquet vs CSV into two fidelity paths and breaks the "one
    normalized answer" property — SUMMARIZE-for-both is the honest, uniform v1.

## [0.34.0] - 2026-06-10

### Added

- **`search(provenance=true)` — a whole-search RO-Crate 1.1 Run Crate.** An opt-in flag that
  attaches `provenance_crate{}` — a single machine-readable manifest documenting an ENTIRE search
  page in one call: the run itself (the query, the sources observed to participate, the ontology
  expansions that fired, and the per-source errors) PLUS per-hit provenance for every result. This
  is the "why an aggregator" artifact — the AI-Act training-data-manifest moment — and it completes
  the **B10 flagship** (B10a per-record dossier + B10b whole-search Run Crate).
  - **Run-level provenance as a `CreateAction`.** The new pure `run_crate.render(result)` emits a
    flat RO-Crate 1.1 `@graph` whose root `Dataset` (`name "Search run: <query>"`) `hasPart`s the
    hits and `mentions` a `#search-action` `CreateAction` (`instrument`→the `data-aggregator-mcp`
    `SoftwareApplication` agent carrying `__version__`, `object`→`./`). The action encodes the
    `query`, `result_count`/`total`, `sources_queried` (the union of sources that returned a hit and
    sources that errored — honestly NOT the full configured adapter set, which the result does not
    recover), the `ontology_expansions` that fired (one object per `taxon`/`mesh`/`tissue`/
    `chemical`/`assay` axis, naming the input, ontology id, canonical name, and synonyms added), and
    the per-source `errors` verbatim — a partial search is DISCLOSED, not hidden.
  - **Per-hit signals reuse the B10a dossier helpers.** Each `#hit-i` `Dataset` carries
    version-currency (B1), licence + normalized SPDX (B3), and FAIR (B4) assessment entities,
    composed by REUSING `dossier.assessment_entities(hit, id_prefix=f"hit-{i}-")` — the dossier's
    five per-signal helpers gained an `id_prefix` parameter (default `""`, so B10a output stays
    byte-identical) and a public `assessment_entities` reuse seam. FAIR is computed per hit via the
    pure `fair.assess`, so `render` stays PURE — no network/file I/O.
  - **No per-hit retraction, no per-hit Crossref.** Search hits carry `trust=None`, so the retraction
    helper emits NOTHING (an honest absence, never a "not retracted" claim). The run crate does NOT
    fan out N Crossref calls; per-hit retraction stays a per-record opt-in via
    `resolve(format=provenance)` (B10a).
  - **Intra-page boundary.** The crate documents the search page just returned; a paginated search
    yields one crate per page (stateless, mirroring B7). A cross-page run crate is out of scope.
  - **Opt-in + additive.** Default off; with the flag off, `search` output is byte-identical to
    pre-B10b. `conformsTo` stays ONLY on the metadata descriptor (`https://w3id.org/ro/crate/1.1`) —
    no fabricated profile URI.

## [0.33.0] - 2026-06-10

### Added

- **`resolve(format=provenance)` — a one-call RO-Crate 1.1 data-availability dossier.** An opt-in
  export that renders a single machine-readable artifact COMPOSING every provenance/integrity
  signal the server already computes for one resolved record: version-currency (B1
  `is_latest`/`superseded_by`), licence + normalized SPDX (B3), FAIRness (B4 `fair`), retraction /
  expression-of-concern (`trust`), and the source/DOI/ID chain (`source`, canonical id, `doi`,
  cross-`identifiers`, `accessions`, qualified `links` rel→target). The dossier is attached under a
  new `DataResource.provenance` field. This is the "why an aggregator" artifact for the
  data-provenance moment — we are the single point that holds all of these signals at once.
  - **Plain RO-Crate 1.1, not a Run Crate.** The new pure `dossier.render(resource)` REUSES
    `ro_crate.render` as the base graph (metadata descriptor + root `Dataset` + file entities) so the
    base crate can't drift, then EXTENDS `@graph` with a schema.org `CreateAction`
    (`#provenance-assessment`, `instrument`→the `data-aggregator-mcp` `SoftwareApplication` agent
    carrying `__version__`, `object`→`./`, `result`→the assessment entities) plus one `PropertyValue`
    per PRESENT signal. `conformsTo` stays ONLY on the metadata descriptor
    (`https://w3id.org/ro/crate/1.1`) — no fabricated profile URI. (The "Provenance Run Crate" term
    is the Workflow-Run-Crate profile for workflow executions — the wrong shape for a per-record
    data-availability dossier.)
  - **Unknown is NEVER a negative claim (the honesty contract).** Only signals actually present are
    represented. An unknown retraction (`trust.retracted is None`) is reported as
    "unknown / not checked", NEVER "not retracted"; a definitive `False` may state "no retraction on
    record (Crossref)". An unrecognized licence is "unrecognized", never an invented SPDX. A missing
    version/FAIR/trust signal is OMITTED, not fabricated. `render` is PURE/deterministic — no
    network or file I/O.
  - **One-call completeness.** `format=provenance` AUTO-attaches `fair` (pure local assess) and
    `trust` (one Crossref call) before rendering, so the dossier is whole in one call — REUSING any
    enricher already attached via `fair=true`/`trust=true` (idempotent, no double-compute). It does
    NOT set the `croissant`/`ro_crate` fields; those stay opt-in via their own format values.
  - **Follow-up:** the whole-search **Run Crate** (one call documenting every source queried +
    per-hit provenance for an entire result set) is the chosen next wave, **B10b**. B10a stays
    per-record so each wave is tight and reviewable.

## [0.32.0] - 2026-06-10

### Added

- **`search(chemical=…)` + `search(assay=…)` — recall axes 4 & 5: ChEBI compounds and EDAM
  assays/methods.** Two more ontology-grounded search-input expansion axes, cloning the proven
  `organism=`/`disease=`/`tissue=` shape: a resolved term ANDs its canonical name + exact
  synonyms into the query as an OR-group, so a single keyword recalls every surface form a
  single-source tool would miss. `chemical=caffeine` ANDs in `1,3,7-trimethylxanthine` (…);
  `assay=ChIP-seq` ANDs in `ChIP-sequencing`/`ChIP-exo` (…). Both are echoed for transparency in
  the new `SearchResult.chemical_expansion` / `assay_expansion` fields and round-trip through the
  pagination cursor (a continuation page does **not** re-expand — the echoes are frozen).
  - **ChEBI backs `chemical=`** (EBI OLS `ontology=chebi`). As with UBERON, two client-side
    filters are load-bearing because the OLS params do not self-enforce: `obo_id` must start with
    `CHEBI:` (cross-ontology leak guard), and an **exact** case-insensitive match of the input to
    the label OR a synonym is required (`exact=true` does not hard-filter — `q=aspirin` surfaces
    "aspirin-triggered protectin D1" before the real `aspirin`/`CHEBI:15365`). No exact match →
    no expansion (conservative; never guess a term).
  - **ChEBI synonyms are capped** to `_MAX_SYNONYMS = 12` (canonical always retained). ChEBI
    synonym lists are large (many IUPAC variants); the cap keeps the ANDed OR-group a sane query
    size. UBERON/MeSH need no cap.
  - **EDAM backs `assay=`** (EBI OLS `ontology=edam`, NOT OBI — OBI returns the term with an
    empty `synonym`, i.e. zero recall value). EDAM mixes id-classes (`topic_`/`data_`/`format_`/
    `operation_`); the filter restricts to **`obo_id.startswith("EDAM:topic_")`** — assay/method
    concepts are EDAM _topics_, so `data_`/`format_`/`operation_` ids are rejected.
  - **Fail-LOUD on an OLS error**, in parity with the organism/disease/tissue axes: a ChEBI
    lookup failure is recorded under `errors["chebi"]`, an EDAM failure under `errors["edam"]`,
    and the query runs **un-expanded** (these are search-input expansions, the opposite of a
    fail-soft resolve enricher — a silently-dropped expansion would make the model conclude the
    synonyms found nothing). A _no-match_ is not an error: the query is returned un-expanded with
    nothing recorded. The pure `_pick_*` matchers are deterministic; HTTP failures propagate and
    are NOT cached.

## [0.31.0] - 2026-06-10

### Added

- **`search(collapse_mirrors=true)` — opt-in cross-repo content dedup beyond DOI.** On top of
  the always-on exact-DOI dedup (`router._dedup`), this folds records that are the **same
  dataset deposited under different (or no) DOIs** — e.g. a Zenodo mirror of a figshare
  deposit, GEO↔ArrayExpress — into **one** record, annotating the survivor with the folded
  copies under a new **`mirrors[]`** field (`Mirror{source, id, doi}`). This is the
  aggregator-native moat extension: only possible because we fan out across 12 sources, so a
  cross-repo mirror is structurally invisible to any single-source tool.
  - **Conservative by design (the load-bearing safety decision).** A false merge silently
    hides a genuinely distinct dataset — worse than a missed merge — so collapse is **opt-in**
    (default `false`; DOI dedup unchanged) and the fingerprint is **high-confidence only**.
    Two records merge **iff** they share ANY full `algo:hex` `files[].checksum` (byte-identical
    → definitional identity) **OR** have an identical fingerprint key =
    `(normalized-title, first-author-surname, year)` with **all three present and non-empty**.
    `_normalize_title` lowercases, drops punctuation and collapses whitespace, then compares
    for **exact** normalized equality (never substring, never fuzzy). A title-only match, a
    missing year, or absent creators on either side does **not** merge. When in doubt, no merge.
    - **The fingerprint path requires DIFFERENT sources** (the checksum path stays
      source-agnostic). B7 is _cross-repo_ dedup: two SAME-source records sharing
      title+author+year are almost always **version siblings** (e.g. Zenodo record v1/v2),
      already modeled by version-currency (`is_latest`/`superseded_by`) — folding them as
      mirrors would be wrong. Only a copy in a different repository is a mirror. (Borne out on
      live data: the dominant real fingerprint collision is consecutive same-source Zenodo
      deposits, correctly left unfolded.)
  - **Annotate, never silently drop.** Every folded copy appears in the survivor's `mirrors[]`
    with its `source`+`id`(+`doi`); no record is ever its own mirror. Survivor selection is
    deterministic: DOI-bearing beats DOI-less; among DOI-bearing, a native id beats a
    `datacite:`-prefixed one (same precedence spirit as `_dedup`); first-seen order breaks ties.
  - **Pagination untouched.** `_collapse_mirrors` is a **pure** presentation-layer fold over the
    already-emitted page, run **after** the `consumed`/offset/`next_cursor` accounting, so it
    cannot corrupt offsets or stall pagination — a folded mirror merely makes a page return
    fewer than `size` items. The flag round-trips through the pagination cursor so continuation
    pages keep collapsing.
  - **Honest scope = intra-page, best-effort.** A mirror that lands on a different page (or past
    the `size` cut) is not collapsed — the stateless server holds no cross-page index.
  - **Deferred follow-ups (noted, out of scope):** (1) fuzzy / shingle / near-duplicate title
    matching (false-merge risk; v1 stays exact-normalized-title + author + year, or shared
    checksum); (2) cross-page mirror collapse (needs state the stateless server doesn't hold);
    (3) default-on collapse (stays opt-in until the fingerprint proves low-false-merge in the field).

## [0.30.0] - 2026-06-10

### Added

- **`resolve(use=<intent>)` — licence-compatibility preflight.** A new opt-in enricher
  attaches a `license_compat{}` advisory to a resolved record: an **ALLOW / REVIEW / DENY**
  verdict for an intended use, naming the governing licence clause and the normalized SPDX
  id. Supported intents are `commercial`, `redistribute`, `modify`, and `ml-training`
  (training is treated as a derivative + commercial use — `commercial-use` + `modifications`
  — which is **our stated interpretation**, documented in the module). The verdict is a
  **pure, local function** (no network call, unlike `trust`) over a **bundled licence
  matrix** whose permission/condition/limitation flags are drawn verbatim from the
  [choosealicense.com](https://github.com/github/choosealicense.com) flag vocabulary
  (vendored into Licensee → GitHub's Licenses API), fetched 2026-06-10, keyed on SPDX id.
  `normalize_spdx` maps bare SPDX ids, spaced/cased prose ("Apache License 2.0", "CC BY 4.0")
  and Creative-Commons / Open-Data-Commons URLs to a canonical SPDX id. **Honest coverage:**
  an unrecognized or absent licence yields `REVIEW` with `spdx_id=null` (defaults to
  all-rights-reserved) — never a fabricated ALLOW/DENY; a `DENY` always names the missing
  permission and its human clause (e.g. "commercial-use not granted (NonCommercial)"); a
  copyleft `same-license`/`disclose-source` obligation downgrades an otherwise-ALLOW
  `redistribute`/`ml-training` to `REVIEW`. An unknown intent fails **loud** (`ValueError`).
  Every verdict carries a **not-legal-advice** disclaimer: it is a metadata-derived
  compatibility _advisory_, not a legal determination. Resolve-only for v1 (parity with
  `trust`/`fair`).
  - **Deferred follow-ups (noted, out of scope):** (1) Croissant `usageInfo` → full
    `odrl:Offer` upgrade — the `LICENSE_MATRIX` built here is the reusable backend, but
    emitting structured ODRL triples would reverse B2's `test_no_odrl_permission_keys_in_b2_output`
    pin and belongs with the B10 dossier; (2) a search-time `use=` filter / advisory column
    across a whole result set (a `strict_license` enforced-fetch gate is B12).

## [0.29.0] - 2026-06-10

### Added

- **`resolve(fair=true)` — RDA-grounded FAIRness score.** A new opt-in enricher attaches
  a `fair{}` assessment to a resolved record: a 0–100 overall score plus
  `findable`/`accessible`/`interoperable`/`reusable` sub-scores, an `assessed` count, and
  a list of actionable `gaps`. Scoring is a **pure, local function** over the normalized
  `DataResource` (no network call), grounded in the machine-evaluable subset of the
  [RDA FAIR Data Maturity Model](https://doi.org/10.15497/rda00050) (Specification &
  Guidelines v0.90). Each gap names its RDA indicator id (e.g. `RDA-R1.1-03M`) and is
  framed as a metadata-exposure gap, never a value judgement about the dataset. Only
  indicators evaluable from the metadata we hold are scored — `assessed` reports exactly
  how many, so the score never fabricates a pass/fail for what the metadata cannot show.
  Licence presence (`RDA-R1.1-01M`) and machine-readability (`RDA-R1.1-03M`) are scored
  as **distinct** indicators: a free-text licence ("see LICENSE.txt") passes the former
  and fails the latter, while an SPDX/CC id ("cc-by-4.0", "MIT") passes both. Score math:
  per-dimension = `round(100 * passed-weight / total-weight)` with priority weights
  Essential=3 / Important=2 / Useful=1; overall = `round(mean of the 4 dimensions)`.
  Resolve-only for v1 (parity with `trust`); search-time FAIR is a deferred follow-up.

## [0.28.0] - 2026-06-10

### Changed

- **Croissant export upgraded to Croissant 1.1** — the `format=croissant` manifest now
  carries the `conformsTo` version marker (`http://mlcommons.org/croissant/1.1`) and a
  `@context` that declares the `dct`/`prov`/`odrl` namespace prefixes. The export gains
  dataset-level **PROV-O provenance** populated from our cross-source enrichment:
  `prov:wasAttributedTo` (creators, with an ORCID `@id` when known) and
  `prov:wasDerivedFrom` mapped **conservatively** from only true-derivation link rels
  (`is_derived_from`/`is_version_of`/`is_new_version_of`) — `is_supplement_to`/`cites`/
  `part_of` are deliberately NOT mapped, so the manifest never overstates provenance.
  Also now emits the schema.org fields we already hold: `keywords` (subjects),
  `dateModified` (last-updated), `publisher` (source display-name), and `citeAs` (only
  when the record was resolved with a bibtex citation). A minimal honest `usageInfo`
  license pointer is added when a license is present; the full ODRL `odrl:Offer` policy
  is deferred to B3 (license-compatibility) — B2 asserts no permissions. The renderer
  stays a pure transform (no I/O). Still file-level: no RecordSet/Field structures.

## [0.27.0] - 2026-06-10

### Added

- **UBERON tissue query-expansion** — `search(tissue=<name>)` resolves a tissue/anatomy
  name to its canonical UBERON term via the EBI OLS4 search API (`ontology=uberon`,
  `exact=true`) and expands the query with the canonical label plus its exact synonyms
  (e.g. `liver` also matches `iecur`/`jecur`). The third ontology-grounded recall axis
  after `organism=` (NCBI Taxonomy) and `disease=` (MeSH), and the first backed by a
  non-NCBI client. Especially additive for the single-cell sources (CELLxGENE/DANDI).
  Two client-side filters are load-bearing (neither OLS param self-enforces): the result
  must be a real `UBERON:` term (a bare relevance search leaks cross-ontology `PR:` hits)
  and the input must match the canonical label or an exact synonym (no expansion into a
  wrong term — a no-match yields no expansion). The expansion is echoed in
  `tissue_expansion`, composes with `organism=` and `disease=` (three AND-groups stack),
  and is fail-loud (a UBERON lookup failure surfaces in `errors["uberon"]` and the query
  runs un-expanded).

## [0.26.0] - 2026-06-10

### Added

- **MeSH disease query-expansion** — `search(disease=<name>)` resolves a disease/phenotype
  name to its canonical MeSH descriptor (NCBI E-utilities, `db=mesh`) and expands the query
  with the canonical descriptor name plus entry-term synonyms (e.g. `breast cancer` also
  matches `Breast Neoplasms`). True added recall the keyword window can't reach, grounded in
  a real ontology — the same shape as `organism=` taxonomy expansion. The expansion is echoed
  in `mesh_expansion`, composes with `organism=` (both AND-groups stack), and is fail-loud
  (a MeSH lookup failure surfaces in `errors["mesh"]` and the query runs un-expanded). The
  `[MeSH Terms]` field restriction collapses a lay name to its one canonical descriptor.

## [0.25.0] - 2026-06-10

### Added

- **MCP resources** — resolved records and the source catalog are now addressable as
  MCP resources (a separate primitive from tools). A client can read any record by URI
  `dataresource://record/{id}` (where `{id}` is the same source-prefixed id the `resolve`
  tool accepts, URL-encoded) and the source catalog at `dataresource://catalog`. Backed
  by the existing resolve pipeline; the `resources` capability is now advertised.

## [0.24.0] - 2026-06-10

### Added

- **Retraction trust signal** — `resolve(trust=true)` now attaches a `trust{}` block
  (`retracted` / `retraction_doi` / `concern`) derived from a single Crossref
  `/works/{doi}` lookup of the record's DOI. A DOI Crossref does not register (e.g. a
  DataCite data DOI) leaves `retracted=null` (unknown — never a false "clean" claim);
  a found-but-clean work is `retracted=false`. As an opt-in resolve enricher it is
  fail-soft (a Crossref outage degrades to unknown, never aborts the resolve). First of
  the Phase-4 trust signals (CoreTrustSeal / FAIR proxy deferred); reinforces the
  verified-fetch / anti-hallucination posture — callers can flag retracted records
  before handing them downstream.

## [0.23.0] - 2026-06-10

### Added

- **CZ CELLxGENE Discover** source — single-cell datasets via the Discover curation
  REST API. The collection is the resource unit (one publication DOI per collection);
  search filters client-side on each collection's tissue/disease/organism/assay
  ontology labels, and `resolve` attaches the H5AD/RDS download manifest (capped at
  200 files; direct URLs, unverified — the API exposes filesize but no checksum).
  `kind="dataset"`.

## [0.22.0] - 2026-06-10

### Added

- **DANDI Archive** source — neurophysiology dandisets (NWB) via the DANDI REST API:
  search + resolve, with a per-asset download manifest (capped at the first 100
  assets; 302→S3, unverified). DOI is attached from the published version's
  metadata (drafts have none). `kind="dataset"`.
- **OpenNeuro** fetch — OpenNeuro datasets (`10.18112/openneuro.*`) are now
  fetchable: discovery rides the existing DataCite firehose, and `resolve` attaches
  the snapshot's top-level file manifest via the OpenNeuro GraphQL API.

## [0.21.0] - 2026-06-10

### Added

- **RCSB PDB** source — macromolecular-structure discovery via the RCSB full-text
  search API, hydrated with title + primary-citation DOI/PubMed in one GraphQL
  batch call; `.cif`/`.pdb` structure files stream from files.rcsb.org (unverified —
  no upstream checksum). `kind="dataset"`.
- **GWAS Catalog** source — genome-wide association studies keyed by disease trait
  (EBI REST), carrying the PubMed cross-link for the paper↔data bridge.
  Discovery-only this wave (summary-statistics fetch deferred). `kind="study"`.
- **OpenML** source — machine-learning datasets via name-substring search; resolve
  attaches an md5-verified ARFF and the auto-converted Parquet, which is operable
  (schema/preview/head/sql via the `[operate]` extra). `kind="dataset"`.

### Changed

- **mypy is now a blocking CI gate.** Added mypy (dev dep + `[tool.mypy]` config) and a blocking `Types (mypy)` step to CI. Cleared the existing type debt with real `None`-handling fixes (narrowing asserts that match documented invariants + a walrus binding); no `# type: ignore` needed. No runtime change.

## [0.20.0] - 2026-06-02

### Added

- HuggingFace datasets are now operable via the datasets-server auto-converted
  Parquet: `huggingface.resolve()` surfaces those files (`source="hf-datasets-server"`),
  so `operate` (schema/preview/head/sql) reaches datasets stored as JSON/JSONL/arrow,
  not only ones that ship `.parquet` at the raw URL. Best-effort: a dataset with no
  converted view (gated/too-big/pending) keeps its raw siblings unchanged.

## [0.19.0] - 2026-06-01

### Added

- **`operate` tool (5th tool)** — inspect/query a remote tabular file (Parquet/CSV/TSV) without downloading it: `op="schema"` (columns+types), `"preview"` (sample), `"head"` (first n rows), `"sql"` (read-only SELECT against the file as the view `data`). Addresses a file by catalog id + file name. Requires the optional `[operate]` extra (`duckdb`/`pyarrow`/`fsspec`); the base install is unchanged.
- **`DataResource.access_modes`** — best-effort capability claim (`fetch` + operate modes), populated on `resolve`, degrading to `["fetch"]` when the `[operate]` extra is absent; `list_sources` flags `operable` sources.

### Security

- `operate(op="sql")` runs user SQL in a locked-down DuckDB: read-only, `disabled_filesystems='LocalFileSystem'` (httpfs only), `lock_configuration`, single-SELECT validation, plus row/byte/wall-clock caps.

## [0.18.0] - 2026-05-31

### Added

- **DataONE** source — eco/environmental federation (KNB, Arctic Data Center, PANGAEA, …) with verified fetch: data objects stream from Member Nodes with per-object MD5/SHA-256 checksum verification.
- **OmicsDI** source — proteomics/metabolomics discovery, restricted to the mass-spec modality repos (PRIDE, MassIVE, MetaboLights, Metabolomics Workbench, GNPS, PeptideAtlas) not already covered by the omics leg.
- **PRIDE** and **MetaboLights** fetch backends — `omicsdi:pride:*` / `omicsdi:metabolights_dataset:*` records fetch end-to-end over the EBI HTTPS mirror (unverified: no upstream checksum; PRIDE is size-checked). Other OmicsDI repos are discovery-only and fail loud at fetch with a source pointer.

### Fixed

- DataONE resolve follows the `/cn/v2/resolve/` **303** correctly: the Member-Node url is read from the `Location` header instead of chasing the redirect into the object bytes (which broke checksum verification). Live-validated end-to-end.
- MetaboLights file urls are sourced from the FTP directory listing, not the WS `/files` API, whose logical names don't always match the physical FTP filename (assay files 404'd).

### Notes

- OmicsDI contributes first-page results only (modality post-filtering precludes stable pagination).
- No dedup-ranking change: the existing binary rule already keeps the verified copy on every realistic DOI collision.

## [0.17.0] - 2026-05-31

### Added

- Structured-output round-trip gate (`tests/test_output_schema_gate.py`) — every
  tool's output is validated against its declared `outputSchema`, guarding against
  field drift between `model_dump()` and `model_json_schema()`.
- `DataResource.metrics` (citations/views/downloads/likes — separate axes, no
  blended score), populated from DataCite inline counts and HuggingFace
  downloads/likes.
- `DataResource.is_latest` / `superseded_by`, derived from version relations in
  `links[]` (fields only; no ranking change).
- `DataResource.last_updated` freshness (DataCite + HuggingFace).
- Tool annotations (`readOnlyHint` on search/resolve/list_sources; explicit
  read/destructive/idempotent hints on fetch).
- MCP prompts: `find_data`, `data_behind_paper`, `search_resolve_fetch`.
- Export: `resolve(format="croissant")` (file-level Croissant) and
  `resolve(format="ro-crate")` (RO-Crate 1.1).

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

## [0.15.0] - 2026-05-31

### Added

- HuggingFace datasets as a search/resolve/fetch source (`hf:<owner>/<name>`). Files
  are fetchable via the HF resolve URL (unverified — the API exposes no checksum/size).
  HF contributes to the first results page only (its API paginates by cursor, not offset).

## [0.14.0] - 2026-05-31

### Added

- `fetch` now downloads a resource's files in parallel (bounded concurrency).
- `fetch` resume — files already present and verified (by checksum, else size) are
  skipped and reported in `FetchResult.resumed`; a re-run is idempotent. `force=true`
  re-downloads everything.
- `fetch` emits MCP progress notifications as files complete when the caller supplies
  a `progressToken`.

## [0.13.0] - 2026-05-31

### Changed

- **Breaking:** `creators` is now a list of `{name, orcid}` objects (was a list of
  name strings). ORCID iDs are populated from DataCite `nameIdentifiers` and Zenodo
  creator metadata where available.

### Added

- `funding` — funding references (`{funder, award}`) from DataCite `fundingReferences`
  and Zenodo `grants`.
- Related-identifier `links` — DataCite `relatedIdentifiers` / Zenodo
  `related_identifiers` are surfaced as `links` (verbatim targets; no graph traversal).

## [0.12.0] - 2026-05-31

### Added

- `search` pagination — an opaque `next_cursor` walks past the first page of
  merged results; pass it back as `cursor` to fetch the next page (per-source
  offsets are packed into the token; `size` stays "deduped results per page").
- `search` filters — `published_after` / `published_before` (publication-year
  bounds) and `kind` constrain results. Filtering is applied to the fetched
  window on normalized fields; a record with no year is dropped when a year
  bound is set.
- `list_sources` now advertises `published_after` / `published_before` / `kind`
  / `cursor` in each source's `filters_supported`.

## [0.11.0] - 2026-05-29

### Added

- `fetch(extract=true)` — opt-in unpacking of downloaded zip/tar archives,
  guarded against path-traversal and runaway extracted size.
- `fetch` integrity check — an unverified `pdf`/`xml` download whose body is
  HTML (a login/paywall page) now fails loud instead of saving a bogus file.
- `resolve` of a Zenodo DOI via DataCite now populates `files[]` (delegates to
  the native Zenodo adapter); such ids are fetchable.
- BioProject `resolve` attaches `links[]` to its SRA runs.
- PubMed `resolve` populates the article abstract (`description`) and, for
  PMC open-access records, `access`/`license` (from EuropePMC/Unpaywall).
- `list_sources` reports per-source fetchability, id examples, and the
  `organism` filter.

### Fixed

- HTTP boundary now fully honors the fail-loud contract: transport-level errors
  (connect/read/timeout) and malformed HTTP-200 bodies (NCBI throttle envelopes)
  are retried and surface as a typed `DataAggregatorError`, on both the
  search/resolve path (`_http`) and the `fetch` streaming path.

## [0.10.0] - 2026-05-29

### Added

- Literature `resolve` (`pubmed:`/`openaire:`) attaches an open-access full-text
  file via an EuropePMC `fullTextXML` → Unpaywall `url_for_pdf` cascade (first
  hit wins; `FileEntry.source` labels the origin). Enrichment — fails soft.
- `DataResource.identifiers` — normalized `{pmid, pmcid, doi}` cross-identifiers.
  PubMed gets them free from esummary; OpenAIRE via the NCBI ID Converter.
- `FileEntry.source` — provenance label for an attached file.
- `pubmed:`/`openaire:` are now fetchable: `fetch` streams open-access full text
  (unverified — no upstream checksum, like GEO). Fails loud when a paper has no
  open full text.

### Changed

- New env var `UNPAYWALL_EMAIL` enables the Unpaywall fallback leg (the EuropePMC
  leg needs no key). `NCBI_EMAIL`/`UNPAYWALL_EMAIL` is forwarded to NCBI idconv.

### Notes

- Cascade deviates from the umbrella spec's literal "PMC → EuropePMC → Unpaywall":
  PMC's machine download is tgz-over-FTP (not HTTPS-fetchable), and EuropePMC
  already serves the PMC OA subset as HTTPS XML — so the dedicated PMC leg is
  dropped. Honors the spec's intent (open full text, first hit wins).
- No MeSH (ceded to the openalex MCP). Full text is open-access only; paywalled
  content is never bypassed.

## [0.9.0] - 2026-05-29

### Added

- `resolve(id, cite=<format>)` renders a citation onto the record — `bibtex`,
  `ris`, `csl-json`, or any CSL style name (`apa`, `mla`, `vancouver`, …). DOI
  records use DOI content negotiation (CrossRef + DataCite); non-DOI records
  produce CSL-JSON from metadata. Default off; failures degrade quietly.
- `DataResource.access` — normalized access status
  (`open`/`embargoed`/`restricted`/`closed`/`unknown`), populated from Zenodo
  `access_right`, OpenAIRE `bestAccessRight`, and an open-license signal on
  DataCite rights.
- `DataResource.citation` — holds the rendered citation when `cite=` is used.

### Changed

- OpenAIRE records now carry `license` (from the deposit instance) and `access`.

### Notes

- PMC license/access for `pubmed:` records is deferred to Phase 9 (bundled with
  PMC full-text retrieval). GEO/SRA/BioProject expose no rights → `access` stays
  null honestly.

## [0.8.0] - 2026-05-29

### Added

- DataCite-repo fetch: resolving a DataCite DOI now attaches `files[]` from the
  host repo's native API — **Figshare** (md5), **Dataverse** (Harvard default,
  `DATAVERSE_BASE_URL` override; md5), **OSF** (osfstorage, paginated; md5), all
  fetchable and checksum-verified. **Dryad** is manifest-only (names/sizes/
  sha-256) — its downloads are token/bot-challenge gated, so it is excluded from
  the fetch allowlist and fetching a Dryad DOI fails loud.
- New per-repo resolver modules: `figshare.py`, `dataverse.py`, `osf.py`, `dryad.py`.

### Changed

- The fetch allowlist accepts `datacite:` ids; fetchability is then decided
  post-resolve from the detected host repo (`_DATACITE_FETCHABLE`).

### Fixed

- DataCite source detection now recognizes Harvard Dataverse (client id
  `gdcc.harvard-dv`, which contains no "dataverse" substring).

## [0.7.0] - 2026-05-29

### Added

- Omics fetch: `fetch` now downloads SRA FASTQ files (via the ENA manifest,
  md5-verified) and GEO supplementary files (parsed from the GEO `suppl/`
  directory index; unverified — NCBI exposes no checksums there).
- New `geo.py` supplementary-file resolver; `geo:` resolves now populate
  `files[]`. A GEO record with no `suppl/` directory (HTTP 404) degrades to
  `files=[]` rather than failing.

### Changed

- The `fetch` tool resolves through `router.resolve` (source-agnostic) instead
  of a hardcoded Zenodo path; the `_FETCHABLE_SOURCES` allowlist now includes
  `sra:` and `geo:`.

## [0.6.0] - 2026-05-29

### Added

- Packaging for publication: `python -m data_aggregator_mcp` entry point,
  complete `[project.urls]` + `keywords`, Beta classifier.
- `server.json` for the official MCP registry
  (`io.github.musharna/data-aggregator-mcp`) + the `mcp-name:` ownership marker
  in the README.
- GitHub Actions: `ci.yml` (pytest + ruff, Python 3.11/3.12) and `publish.yml`
  (Release-triggered PyPI upload via OIDC trusted publishing — no stored token).
- `PUBLISH.md` runbook and user-facing install/use docs (`uvx`, `pip`,
  `claude mcp add`).

### Notes

- Prepare-to-the-gate: the public GitHub repo, the real PyPI upload, and the
  registry submission are documented manual steps, not executed here.
- HTTP transport remains deferred — distribution is local stdio via PyPI/`uvx`.

## [0.5.0] - 2026-05-28

### Added

- Unifying layer: NCBI-Taxonomy-backed **synonym expansion** on `search`. New
  optional `organism` param — resolves to a taxid and ANDs the query with the
  canonical name + synonyms (e.g. `Orobanche aegyptiaca` also matches
  `Phelipanche aegyptiaca`, taxid 99112). The expansion is echoed in
  `SearchResult.taxon_expansion`.
- **Organism normalization**: results/resolved records gain `taxa[]`
  (`{taxid, name}`) derived from raw `organism[]` via NCBI Taxonomy; raw strings
  are preserved.
- **Cross-links**: a `described_in` → `plant-genomics:taxid:<n>` link is attached
  for Viridiplantae (plant) taxa, the seam to the sibling `plant-genomics-mcp`.

### Notes

- No new search source and no new tool — Phase 5 is a taxonomy module plus a
  post-merge enrichment pass. `fetch` is unchanged (Zenodo-only).
- Enrichment incurs zero taxonomy calls for records without an organism. A
  taxonomy outage surfaces in `errors["taxonomy"]` on `search` (never silently
  dropped) and degrades gracefully on `resolve`.

## [0.4.0] - 2026-05-28

### Added

- Unified `literature` source: PubMed + OpenAIRE publication discovery, fanned
  out in parallel and merged. Registered as a fourth `search` source.
- Resolve-time paper→data links: resolving a `pubmed:` id attaches `links[]` to
  `sra:`/`geo:`/`bioproject:` ids via NCBI elink; resolving an `openaire:` id
  attaches `datacite:` links via the ScholeXplorer Scholix API. Publication↔
  publication citation edges are dropped — that is the standalone openalex MCP's
  job, not ours.

### Notes

- Literature is discovery-only — `fetch` stays Zenodo-only and fails loud for
  `pubmed:`/`openaire:` ids.
- OpenAIRE paper→dataset Scholix links are sparse (most paper edges are
  citations, which are dropped); the PubMed→GEO/SRA elink path is the reliable
  paper→data bridge. OpenAIRE's contribution is discovery breadth.

## [0.3.0] - 2026-05-28

### Added

- Unified NCBI omics source: GEO + SRA + BioProject discovery via E-utilities,
  fanned out internally and merged. Registered as a third `search` source.
- ENA filereport FASTQ manifest attached on `resolve` of an `sra:` id (direct
  https URLs).
- Optional `NCBI_API_KEY` env var to raise the NCBI rate limit (3→10 req/s).
- Shared round-robin `_merge.interleave` (extracted from the router) so the
  omics fan-out reuses fair merging.

### Notes

- Omics fetch is deferred — `fetch` remains Zenodo-only and fails loud for omics
  ids. GEO/BioProject are discovery-only (no file manifest in this phase).

## [0.2.0] - 2026-05-28

### Added

- DataCite discovery adapter — one query spans every DataCite client (Dryad,
  Figshare, Dataverse, OSF, Mendeley, …); metadata-only, so resources carry no
  file manifest.
- Multi-source router: `search` fans out across Zenodo + DataCite in parallel,
  round-robin merges results so the page limit never starves a later source,
  dedups by DOI (native fetch backends win over DataCite metadata), and surfaces
  per-source failures in `errors{}` instead of silently dropping a backend.
- `search` `sources` filter to restrict fan-out (e.g. `["datacite"]`).
- Shared `compact()` helper in `models` (extracted from the Zenodo adapter).

### Changed

- `resolve` routes by id shape (`zenodo:` / bare id / `datacite:` / bare DOI).
- `fetch` is Zenodo-only in Phase 2 and fails loud (`FetchNotSupportedError`)
  for discovery-only sources; per-repo fetch adapters come in a later phase.

## [0.1.0] - 2026-05-28

### Added

- Initial MCP server: search/resolve/fetch/list_sources over Zenodo.
- Normalized DataResource model; stream-to-disk fetch with max_bytes guard,
  checksum verification, and provenance sidecar.
