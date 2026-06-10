# One-Stop-Shop for Data — Master Implementation Plan

> **For agentic workers:** This is a PROGRAM plan (a sequenced set of independently-shippable tiers), not a single bite-sized task list. Per `superpowers:writing-plans` decomposition guidance, each tier below becomes its **own** detailed `docs/superpowers/plans/<date>-<tier>.md` (bite-sized TDD steps) at build time, executed via `superpowers:subagent-driven-development`. This document fixes scope, sequence, contracts, risks, and the cross-cutting decisions.

**Goal:** Evolve data-aggregator-mcp (v0.16.0) from a find+fetch aggregator into a "one-stop shop for data" — deep-first: verified-fetch + operate-on-data-in-place + trust signals + broader (evidence-picked) source coverage + best-in-class MCP-citizen polish — while protecting the moat (normalized cross-source unification, DOI dedup, taxonomy expansion, paper↔data bridge).

**Architecture:** Keep the normalized `DataResource` contract + the parallel search fan-out → dedup → enrich → resolve → fetch pipeline (`router.py`, `server.py`). Add capabilities by (a) enriching `DataResource` fields, (b) adding MCP primitives that are NOT tools (prompts, resources), (c) adding ONE new tool for the operate-on-data verb class, (d) adding source adapters behind the existing `_ADAPTERS` registry, and (e) one federated-fetch leg (DataONE). stdio stays default; HTTP transport is a deferred, opt-in scope-call.

**Tech stack:** Python 3.11+, `httpx` (async), `pydantic` v2, `mcp` SDK (stdio), `pytest`/`pytest-asyncio`/`pytest_httpx`. New deps introduced only where a tier needs them (e.g. `duckdb`, `pyarrow`/`fsspec`, `htsget`/`pysam`), each gated so the base install stays light.

**Strategic thesis (why this order):** the demand evidence (round-2 research) is that deep-research products _find papers, not data_ and silently hallucinate on fetch-fail; MCP is the data socket shipped empty. So the highest-value work is deepening verified-fetch + operate-on-data + trust, and positioning as the drop-in data MCP — not breadth-chasing. Breadth is added by evidence and by leverage (federation), with ToS as a hard filter.

**Source research:** [[data_aggregator_competitive_analysis_v3_2026-05-31]] · [[data_aggregator_deep_expansion_plan_2026-05-31]] · [[data_aggregator_source_coverage_2026-05-31]] (in `~/.claude/projects/-home-mjarnold/memory/`).

---

## ⚠ BUILD STATUS as of 2026-06-10 (live-audited; AUTHORITATIVE — read this FIRST)

> **This plan ran ~3 weeks ahead of itself: most of it is already shipped.** The repo (v0.23.0) is far
> ahead of the prose below — the prose preserves the original rationale/sequence, but for "what's left"
> trust THIS table, then re-audit live `src/` before scoping anything. (A 2026-06-10 session recommended
> DataONE as "next" straight off the stale sequence — it had shipped 05-31. Don't repeat that.)

**Phase 1 — Complete MCP citizen: ✅ DONE.** schema/output-gate (`tests/test_output_schema_gate.py`); `Metrics`/`is_latest`/`superseded_by`/`last_updated` (`models.py`); tool **annotations** (`server.py` `ToolAnnotations` on all tools); **prompts** (`server.py` `_PROMPTS`); **Croissant** file-subset (`croissant.py`) + **RO-Crate** (`ro_crate.py`); **Scholix**/relationType (`scholix.py`); OpenAIRE (`openaire.py`). _Only residual:_ ROR affiliation enrichment (not built — minor).

**Phase 2 — Operate-on-data: ✅ DONE (v0.19.0).** 5th tool `operate` (DuckDB SQL over remote Parquet, `operate.py`/`duckquery.py`). Tools now = `search, resolve, fetch, operate, search_resolve_fetch` (5).

**Phase 3 — Source breadth: ✅ DONE** (all evidence-picked legs shipped):

- P3.1 DataCite refinements — `resourceTypeGeneral` filter ✅ + per-repo fetch resolvers (figshare/osf/dataverse/zenodo/dryad/openneuro) ✅
- P3.2 DataONE federation ✅ (`dataone.py`, checksummed fetch; **PANGAEA + KNB + Arctic + TERN reachable through this federation** — P3.4 PANGAEA needs no standalone adapter)
- P3.3 OmicsDI + PRIDE + MetaboLights ✅ — P3.5 PDB/GWAS/OpenML ✅ — P3.6 OpenNeuro/DANDI ✅ — single-cell **CELLxGENE** ✅ (v0.23.0)
- P3.7 non-research (EDGAR/patents/legal) — **carved to a sibling server** per decision Q2; NOT core.

**Distribution (Q1, pulled forward): ✅** registry/Glama/awesome-mcp listings shipped (see MEMORY.md da-mcp section).

**Phase 4 — Frontier: ☐ THE ACTUAL OPEN WORK.** All four are genuinely unbuilt (0 code, live-confirmed 06-10):

- **P4.2 MCP resources** (`data://record/{source}/{id}`) — no `list_resources`/`read_resource` registered. _Smallest, protocol catch-up._
- **P4.4 Trust signals tier-2** — Crossref Retraction-Watch flag + CoreTrustSeal `host_certified`. _Cheapest moat-reinforcer (anti-hallucination narrative); verify the retraction field on a real retracted DOI first._
- **P4.3 Async long-fetch** (`fetch` `mode`/`job_id`) — `fetch.py` has no job model. _Larger; unlocks IPUMS/SWH/GBIF._ **DEFERRED (decided 2026-06-10):** this capability IS the MCP **Tasks** extension (SEP-1686→SEP-2663), which is only _experimental_ in SDK 1.27.2 ("may change without notice") and **finalizes 2026-07-28**. Building a bespoke `job_id` now = throwaway; building on the experimental surface = churn risk. **Revisit after 2026-07-28** and build on the finalized Tasks extension (confirms the line-210 lock). Do P4.1 first.
- **P4.1 True semantic RECALL** — only the v0.16 window re-rank exists (`embeddings.py`); beyond-window retrieval is the big lift. _Largest; partially cedeable to openalex._

→ **Recommended next:** P4.4 (retraction trust signal) or P4.2 (resources) — both small, both reinforce the moat/protocol. Full detail in the Phase-4 section below.

---

## Current-state corrections (verified live this session — supersede the research memos where they conflict)

The research agents made two assumptions that the live code contradicts. The plan is written to the code, not the memos:

1. **`outputSchema` is ALREADY declared on all 4 tools** (`server.py:252,282,321,343` — `SearchResult`/`DataResource`/`FetchResult`/inline). The research "Tier-1: add structured output (A1)" is therefore _mostly shipped_. Residual = confirm the MCP SDK actually emits `structuredContent` alongside the text block on each call, and add a regression test. Treat as **verify**, not build.
2. **The DataCite search leg is ALREADY a generic firehose** — `datacite.py:1-4`, `search()` issues `GET /dois?query=...` which "spans every DataCite client (Dryad, Zenodo, Figshare, Dataverse, OSF, Mendeley, …)". There are NOT "5 named search legs" to collapse — there is **one** generic DataCite leg plus a separate native `zenodo` leg (kept for fetch). So the source-coverage "firehose" headline is **already true for discovery**. The real residual is (a) optional `resourceTypeGeneral` filtering on that leg, and (b) more per-repo _fetch_ resolvers (today only figshare/dataverse/osf/zenodo fetch; dryad manifest-only).

Other verified ground truth used below:

- `DataResource` (`models.py:58-77`) has **no** `metrics`, **no** `access_modes`/`queryable`, **no** `last_updated` field — those are genuine additions. `kind` is a fixed string set `{dataset, sequencing_run, study, publication, software}` (`router.py:35`); new source types may need new `kind` values (a contract decision).
- `links[]` already parses `relatedIdentifiers` into `{rel, target_id}` with snake-cased relation types (`datacite.py:157`, `models._rel`) — B2 (is_latest/superseded) is post-processing of data we already hold.
- No tool **annotations**, no **prompts**, no **resources** are registered (`server.py` defines only `TOOLS` + `_list_tools`/`_call_tool`). Those are real adds and do NOT count against the "4-tool" ceiling (separate MCP primitives).
- `_ADAPTERS` registry (`router.py:41`) = `{zenodo, datacite, omics, literature, huggingface}`; new sources slot in here; registration order = dedup precedence.

---

## Phase 1 — Complete MCP citizen + confident-pick layer (no new tool, extends existing contracts)

**Goal:** Ship the cheap, high-leverage, zero-scope-call wins: finish MCP-primitive polish, attach trust/quality signals (mostly free-in-metadata), and emit Croissant. All extend existing contracts; no CEDED conflict.

**Items:**

- **P1.1 — verify/finish structured output.** Confirm `structuredContent` is emitted for the live `outputSchema`s; add a test asserting the shape on a `search`/`resolve` call. (Correction #1.)
- **P1.2 — tool annotations.** Add `annotations={readOnlyHint: true}` to `search`/`resolve`/`list_sources`; `fetch` is read-only w.r.t. user state (writes only to a dest the caller names) — annotate accordingly. Smooths client auto-approval.
- **P1.3 — server prompts.** Register `@server.list_prompts()`/`get_prompt()` with 2–4 templates: "find data for <topic/organism>", the search→resolve→fetch flow, "get the data behind <paper/DOI>". Surfaces in Claude Code as `/mcp__data_aggregator__*`.
- **P1.4 — `DataResource.metrics{}` + DataCite inline metrics.** Add `metrics: dict` (or a typed `Metrics` model: `citations`, `views`, `downloads`, each int|None, kept as SEPARATE axes — no blended score by default). Populate from DataCite `attributes.{citationCount,viewCount,downloadCount}` in `datacite._normalize`. Add `sort` ∈ {relevance, usage, citations} to `search` (router + server schema + cursor round-trip, mirroring `rank`).
- **P1.5 — `is_latest` + `superseded_by`.** Derive from existing `links[]` relation types (`is_new_version_of`/`is_previous_version_of`/`has_version`/`obsoletes`/`is_obsoleted_by`). Add the two fields to `DataResource`; compute in a post-normalize step. Optionally down-rank superseded records in search ordering.
- **P1.6 — HF metrics.** Map `downloads`/`likes`/`trending_score` from the HF API into `metrics{}` in `huggingface._normalize`.
- **P1.7 — `last_updated` freshness.** Add a normalized `last_updated` field; populate from each adapter's modified/updated field where present.
- **P1.8 — Croissant 1.1 export in `resolve`.** New `croissant.py`; `resolve(format="croissant")` renders a Croissant 1.1 JSON-LD manifest from the resolved `DataResource` (files, checksums, license, access, relations, creators). Extends the existing `resolve(cite=)` rendering pattern. Fail-soft like `cite`.
- **P1.9 — relationType / Scholix alignment + ROR (interop).** Normalize `links[].rel` to the canonical DataCite `relationType` vocabulary (verbatim values) and optionally expose paper↔data links in a Scholix-shaped form; add ROR id extraction for creator affiliations/publishers where the source provides it. (May split P1.9 into its own minor tier.)

**Key files:** `models.py` (new fields/models), `server.py` (annotations, prompts, `sort` schema, `format` arg), `router.py` (`sort`, post-normalize derive, cursor), `datacite.py` (metrics), `huggingface.py` (metrics), new `croissant.py`.
**Contract changes:** additive `DataResource` fields (`metrics`, `is_latest`, `superseded_by`, `last_updated`) — backward-compatible; new `search.sort` + `resolve.format` args.
**Risks:** DataCite metric field names/availability vary — confirm against live records; metrics must stay nullable (not all sources expose them).
**Acceptance:** prompts list in a client; annotations present; a Zenodo/Figshare record carries `metrics`; a versioned record shows `is_latest`/`superseded_by`; `resolve(format=croissant)` validates against the Croissant 1.1 schema; full suite green + live probes.
**Version:** minor bumps per sub-tier (the established P-tier cadence), e.g. v0.17–v0.19.

---

## Phase 2 — Operate-on-data-in-place (the differentiator) — **requires scope-call S1 (5th tool)**

**Goal:** Let an agent preview/query/subset a remote dataset without downloading it — the capability nothing in the MCP ecosystem ships, and the "one-stop shop" payoff. All client-side + stdio-safe.

**Items:**

- **P2.0 — `access_modes` capability field (enabler).** Add `access_modes: list[str]` to `DataResource` (values: `fetch`, `preview`, `schema`, `sql`, `rows`, `region`, …) populated per source+file-format so the agent knows what's operable vs locate-only. The single most important schema change for this phase.
- **P2.1 — the `operate` tool (5th tool).** New tool dispatching by `op` ∈ {`preview`, `schema`, `head`, `sql`, `region`}; takes a resolved id (or DataResource) + op-specific params; returns structured rows/schema/preview (NOT a file path — distinct from `fetch`). **SCOPE-CALL S1: opens ">4 tools".** Recommended over overloading `fetch` (keeps the download-to-path contract clean).
- **P2.2 — DuckDB + httpfs.** `sql`/`rows`/`head` over remote Parquet/CSV with predicate+projection pushdown. One embedded dep, no server. Covers Zenodo/Figshare/Dataverse/OSF/HF tabular files.
- **P2.3 — HF datasets-server.** `rows`/`filter`(SQL-where)/`statistics`/`size` via REST — pure extension of the HF adapter, near-zero deps.
- **P2.4 — Parquet/Arrow footer introspection + CSV/TSV sniff.** `schema`/`preview` from the footer/first-N-KB via range reads (`pyarrow`+`fsspec`).
- **P2.5 — htsget genomic-region slicing.** `region` op for CRAM/BAM/VCF (EGA native; **verify NCBI-SRA support by live probe before advertising**). Gate behind the per-source `access_modes` flag.

**Key files:** `models.py` (`access_modes`), `server.py` (new tool + dispatch), new `operate.py` (op dispatch), new per-technique modules (`duckquery.py`, `htsget.py`, HF datasets-server in `huggingface.py`).
**Contract changes:** new tool; new `DataResource.access_modes`. Tool count 4→5.
**Risks:** dep weight (DuckDB/pyarrow) — gate so base install stays light; htsget needs index files + endpoint availability; large/streamed reads need byte ceilings (reuse `fetch`'s max_bytes discipline).
**Acceptance:** `operate(op=sql)` returns filtered rows from a remote Parquet without full download; `operate(op=region)` slices a CRAM by `chr:start-end` from a live EGA endpoint; `access_modes` correctly reflects per-record capability.
**Version:** v0.20 (or a 1.0 candidate if the tool-surface change is treated as a milestone).

---

## Phase 3 — Source breadth (by evidence + leverage; ToS-filtered)

**Goal:** Add coverage that reinforces the bio + paper↔data moat and extends cleanly into adjacent domains, prioritizing federation leverage and clean model fit. ToS is a hard gate.

**Items (ranked):**

- **P3.1 — DataCite leg refinements.** Add optional `resourceTypeGeneral` filtering to the existing generic leg; add more per-repo **fetch** resolvers (the discovery firehose already exists — Correction #2). PROBE what the generic leg already yields for PANGAEA/OpenNeuro/DANDI/EMPIAR before building bespoke discovery.
- **P3.2 — DataONE federated leg.** New `dataone.py` — the one hub exposing checksums + delivering objects via one API (Solr `getChecksum` + CN/MN `get`). New-domain (eco/environmental), low dedup collision, first-class verified-fetch path. Slots into `_ADAPTERS` + the fetch guard.
- **P3.3 — OmicsDI + direct PRIDE/MetaboLights.** Closes the proteomics/metabolomics modality gap (we have transcriptomics/sequence, not MS). OmicsDI for discovery; direct PRIDE/MetaboLights for verified fetch.
- **P3.4 — PANGAEA.** Via DataCite DOI + OAI-PMH (probe existing-leg yield first); bespoke work is fetch, not discovery.
- **P3.5 — Clean native adapters: RCSB PDB, GWAS Catalog, OpenML.** CC0/CC-BY, DOI+PMID-rich, model-fit-clean; PDB/GWAS strengthen the literature bridge.
- **P3.6 — OpenNeuro + DANDI.** Neuroimaging/neurophysiology; DataCite DOI discovery + bespoke S3/CLI fetch.
- **P3.7 — Non-research public records: SEC EDGAR, PatentsView, CourtListener, Regulations.gov.** US-gov public-domain, record+file shaped, free/trivial-auth. **Contract decision: new `kind` values** (e.g. `filing`, `patent`, `legal_opinion`, `regulation`) vs mapping to existing kinds — affects the `kind` filter enum.

**Out of scope (documented so not re-litigated):** climate cubes (ERA5/NOAA — would need a `datacube` shape + async), coordinate-queried astronomy (MAST/SDSS — VO query model), reference/annotation knowledge bases (UniProt/Ensembl/AlphaFold/ClinVar/Reactome/KEGG — entity-lookup → a sibling "bio-reference" surface, not this discovery model), and all query/stream/graph sources (Wikidata/OSM/census-API/BigQuery/Common-Crawl).
**ToS hard-AVOID (cited):** FRED, crypto APIs, Nasdaq/Quandl, GADM, OSM (ODbL share-alike), Lens.org (verify), Earth Engine, Copernicus, Kaggle, dbGaP/UK-Biobank/PhysioNet-credentialed.

**Key files:** new `dataone.py`, `omicsdi.py`/`pride.py`/`metabolights.py`, `pangaea.py`, `pdb.py`, `gwas.py`, `openml.py`, `openneuro.py`, `dandi.py`, `edgar.py`, `patentsview.py`, `courtlistener.py`, `regulations.py`; `router.py` (`_ADAPTERS`), `server.py` (`_SOURCES`, `_FETCHABLE_SOURCES`, `kind` enum), `models.py` (possible new `kind`s).
**Risks:** `kind`-enum expansion ripples to filters + every source's `kinds`; non-research sources risk diluting the bio/research positioning (scope-boundary decision); per-source ToS must be re-read at build time (memo flags several `[unverified]`).
**Acceptance:** each new source searches + resolves + (where fetchable) fetches with verification; dedup holds across the firehose + new legs; ToS-AVOID list enforced (no adapter for a prohibited source).
**Version:** minor bump per source (or per small batch).

---

## Phase 4 — Frontier + reach (bigger lifts; usage-driven)

**Goal:** The harder, later, demand-gated capabilities.

**Items:**

- **P4.1 — True semantic RECALL.** Beyond the v0.16 window re-rank: LLM query-expansion→structured-filter, or an optional local embedding index, so retrieval can surface records OUTSIDE the keyword window. Large; partially cedeable.
- **P4.2 — MCP resources + resource templates.** Expose a resolved record / fetched file as `data://record/{source}/{id}`. Catch-up to cyanheads; separate primitive (not a tool).
- **P4.3 — Async long-fetch job model.** `fetch` `mode`/`job_id` (start/status/cancel) for large SRA/Figshare pulls; **unlocks IPUMS / Software Heritage / GBIF** (async build/cook/extract). Respects the tool ceiling.
- **P4.4 — Trust signals tier 2.** CoreTrustSeal `host_certified` (static table), retraction flag via Crossref Retraction-Watch (1-call, opt-in; **verify field/relation on a real retracted DOI first**), FAIR proxy sub-score from held metadata (NOT the slow live F-UJI API).
- **P4.5 — Positioning + reach.** Publish the "drop-in data MCP for the deep-research socket" + "verified-fetch-or-fail-loud (anti-hallucination)" narrative (README + writeup). Evaluate **S2 (Streamable HTTP transport, additive, stdio default)** only if hosted demand materializes.

**Version:** v0.2x → 1.0 when the surface stabilizes.

---

## Cross-cutting decisions / scope-calls (resolve BEFORE the relevant phase)

- **S1 — open a 5th tool (`operate`) for operate-on-data.** Gates Phase 2. **Recommendation: YES** — it is the go-deep thesis; one tool dispatched by op-type keeps the surface tight. (Resources/prompts in Phase 1/4 do NOT count — separate primitives.)
- **S2 — Streamable HTTP transport** (additive; keep stdio default; never HTTP+SSE). Gates a hosted offering + the socket positioning. Defer to Phase 4; bring to user only on hosted demand. Inherits OAuth2.1/OIDC + Origin-validation burden.
- **S3 — `kind` enum expansion** for non-research records (filing/patent/legal_opinion/regulation) vs mapping to existing kinds. Gates P3.7. Decision needed (affects the public filter contract).
- **S4 — `metrics` shape** (typed model vs dict) + whether to offer an optional blended `rank_score` (keep axes separate by default).
- **S5 — PID-relation graph one-hop** (`resolve(expand_links=true)`) — citation GRAPH was ceded to the OpenAlex MCP; one-hop relatedIdentifier expansion is arguably distinct. Decide if/when.
- **S6 — non-research scope boundary** — do EDGAR/patents/legal belong in THIS server, or a sibling? (positioning vs breadth.)

---

## Dependency graph & recommended sequence

```
Phase 1 (no scope-call) ──┬─ P1.1–P1.3 MCP polish        (independent, ship first)
                          ├─ P1.4–P1.7 trust metrics      (independent)
                          ├─ P1.8 Croissant export        (independent)
                          └─ P1.9 relationType/ROR        (pairs with P1.5)
        │
        ▼   resolve S1 (5th tool) ──► Phase 2  P2.0 access_modes ─► P2.1 operate tool ─► P2.2–P2.5 techniques
        │                                                            (DuckDB/HF-ds/footer first; htsget gated)
        ▼   resolve S3 (kind enum), S6 (scope) ──► Phase 3  P3.2 DataONE + P3.3 OmicsDI + P3.5 PDB/GWAS/OpenML
        │                                                    (P3.1 DataCite refinements anytime)
        ▼   resolve S2 (transport) if hosted demand ──► Phase 4  recall / resources / async-jobs / positioning
```

**Recommended first build: Phase 1, sub-tiers P1.1–P1.8** (no scope-call, all additive, immediate "confident-pick" + interop + MCP-citizen value). Then bring **S1** to the user and start Phase 2. Phase 3 source breadth proceeds in parallel batches once S3/S6 are decided. Phase 4 is demand-gated.

**Each tier above gets its own detailed `docs/superpowers/plans/` spec (bite-sized TDD steps) at build time — this master plan is the program-level contract, not the step-level one.**

---

## Gap-Analysis Addendum (post-review, 2026-05-31) — amends the tiers above

A 3-agent adversarial review (completeness/web · technical/architecture-code-grounded · scope/strategy) produced the following. Memo: `~/.claude/projects/-home-mjarnold/memory/data_aggregator_master_plan_gap_analysis_2026-05-31.md`.

### Verified corrections (override the tiers where they conflict)

- **htsget is NOT available on NCBI-SRA** (refuted, strong). P2.5 region-slicing = **EGA/ENA-only**; EGA is controlled-access (largely non-redistributable). Do not advertise `region` for SRA. The bio region-slice win is real but smaller.
- **structuredContent is SDK-VALIDATED** (mcp 1.27.2): the dict return is validated against the published `outputSchema` on every call. Additive `DataResource`/`SearchResult` fields are therefore **contract changes that can hard-fail the tool**, not free. The existing `test_all_tool_outputs_validate_against_schemas` does NOT validate against the schema. **P1.1 is upgraded to a standing CI gate: a `model_dump()`↔`model_json_schema()` round-trip validation test, landed BEFORE any new field.**
- **Croissant 1.1 = ODRL + PROV-O + DUO** (not 1.0 JSON-LD), and a full `RecordSet`/`Field` manifest needs per-column structure = the **P2.4 footer-introspection capability**. **P1.8 is re-scoped to a file-level Croissant subset** (FileObjects + dataset props, documented as such); full RecordSet export moves to AFTER P2.4. Add **RO-Crate** as a sibling export (research-output packaging — fits our Zenodo/Dryad/omics corpus better than Croissant's ML focus; same render pattern).
- **DataCite inline metrics CONFIRMED** (P1.4 sound, keep nullable). **Crossref retraction** = `update-to[].type == "retraction"` (P4.4).

### Missed prerequisites (ordered into the phases)

1. dump↔schema round-trip CI test — before ANY new field (Phase 1, item 0).
2. Single-source `kind` enum: derive the `search` inputSchema enum (`server.py`) from `_VALID_KINDS` (`router.py`); a test pins the literal — refactor before P3.7.
3. Per-source precedence RANK replacing the `datacite`-vs-rest boolean in `_dedup` — before DataONE/PANGAEA/PRIDE.
4. Optional-extras packaging (`[project.optional-dependencies]` `operate`/`genomics`) + import-guarded degrade; `access_modes` must drop modes whose extra is absent — before P2.
5. `asyncio.to_thread` (or equiv) for sync heavy libs (DuckDB/pyarrow/pysam) inside the async dispatch — design rule for P2.
6. Secondary dedup key (normalized landing URL / accession) for DOI-less records — before the no-DOI-heavy legs.
7. Croissant conformance-level decision (file-level vs full RecordSet) — settled above (file-level first).

### access_modes & operate (P2) hardening

- `compact()` strips `files[]` from search results, and `mime` is often `None` → format-dependent modes (sql/region/schema) are NOT reliably knowable at search/resolve time. Make `access_modes` **two-tier**: a source-level capability _claim_ (best-effort), with format-dependent modes verified by a cheap HEAD/footer probe inside `operate` and failed loud if the claim doesn't hold.
- `operate` needs its OWN resource limits — `fetch`'s `max_bytes` does not transfer to DuckDB/fsspec. Specify: row cap, result-byte cap, statement + wall-clock timeout.

### Other missed items to fold in

- **DCAT-US generic harvester** for the US-gov long tail (one adapter, DataCite-firehose-style leverage) — relevant to P3.7's sources.
- **MCP 2026-07-28 RC** (locked 2026-05-21): build P4.3 async-fetch on the RC **Tasks** extension (not a bespoke `job_id`); adopt **Server Cards (`.well-known`)** for distribution; confirm nothing built on deprecated sampling/logging/**Roots**; **MCP Apps** could render operate previews. Re-evaluate Phase 4 against the RC.
- **Single-cell atlases (CELLxGENE Census / HCA)** — add to Phase 3 (on-moat bio modality the plan dropped).
- **HF now ships an official MCP server with Dataset Search** — a discovery competitor on the ML slice; account for it in positioning.
- **Retraction-propagation across the paper↔data bridge** — the differentiated version of P4.4.

### Strategic amendments (pending user decision — see Open Questions)

- **C1:** distribution (P4.5/E1-E2 + directory listings) is the competitive memo's #1 gap and is sequenced LAST under a "demand-driven" thesis with ~0 users — **pull it forward to run parallel with Phase 1.**
- **C2:** non-research records (P3.7 EDGAR/patents/legal) dilute the bio identity + force the irreversible kind-enum break — **recommend carving to a sibling server** that reuses `DataResource` as a library.
- **C3:** the thesis's strongest evidence is fetch-failure/hallucination → **fetchable-source breadth (DataONE/OmicsDI) is the better-evidenced first bet; operate-on-data is a strong SECOND**, validated by the usage signal distribution generates.

### Open questions for the user (recommendations in the memo): Q1 distribution timing · Q2 non-research home · Q3 first-build-after-Phase-1 · Q4 kind-enum · Q5 metrics shape · Q6 superseded down-rank · Q7 5th-tool timing · Q8 PID one-hop.

---

## Resolved sequencing (user decisions, 2026-05-31) — AUTHORITATIVE over the phase order above

The gap-analysis open questions were resolved by the user. This section overrides the original Phase 1→2→3→4 ordering where they conflict.

**Q1 — Distribution: PULLED FORWARD, parallel with Phase 1.** Directory listings (biocontext.ai PR + Glama `glama.json`/claim + Smithery + mcp.so) + the unification/taxonomy/bridge positioning writeup run NOW, concurrently with Phase 1. Rationale: it is the competitive memo's #1 gap, cheap, independent, and it generates the usage signal the rest of the plan is steered by. (Formerly P4.5/E1-E2 at the end.)

**Q2 — Non-research sources: CARVED to a SIBLING server.** P3.7 (EDGAR/patents/legal/gov) is REMOVED from the core server and the core `kind` enum stays bio/research-shaped. The sibling reuses `DataResource` as an imported library and evolves on its own ToS/cadence. The "single-source kind enum" refactor (prereq #2) still lands in core for hygiene, but is NOT expanded for non-research kinds.

**Q3 — First deep bet after Phase 1: FETCH-BREADTH (DataONE + OmicsDI), BEFORE P2 operate-on-data.** DataONE = the only federation hub with checksummed fetch; OmicsDI broadens omics discovery. This attacks the fetch-failure/hallucination evidence directly at lower technical risk. P2 (operate-on-data / DuckDB / region-slice) is RESEQUENCED to AFTER fetch-breadth, gated on the usage signal that distribution produces. operate-on-data remains the strong second bet, not the first build.

**Smaller defaults applied (Q5–Q8):**

- Q5 metrics shape → a typed `Metrics` model with **separate axes** (citations/views/downloads), **no blended score** by default.
- Q6 superseded records → add `is_latest`/`superseded_by` **fields only**; **no change to default ranking** (down-ranking is opt-in later, if at all).
- Q7 5th tool (operate verb) → **approved in principle**, built only after Phase 1 + distribution + a usage signal justifies it (i.e. with Q3's reseq).
- Q8 PID one-hop traversal (S5) → **stays ceded** (camel's-nose toward the citation-graph we deliberately gave to the openalex MCP).

### Resulting build order

1. **NOW (parallel):** (a) Distribution — listings + positioning writeup; (b) Phase 1 — schema round-trip CI gate (item 0) → metrics/`is_latest`/`last_updated` fields → annotations/prompts → Croissant **file-level** subset + RO-Crate → relationType/Scholix/ROR.
2. **Next deep bet:** fetch-breadth — per-source dedup **rank** prereq → DataONE (checksummed) → OmicsDI.
3. **Then, usage-gated:** P2 operate-on-data (optional-extras packaging + `asyncio.to_thread` + own resource limits + claim-then-probe `access_modes` prereqs first), full Croissant RecordSet after the P2.4 footer capability.
4. **Throughout:** re-evaluate Phase 4 async-fetch against the MCP 2026-07-28 RC Tasks extension; adopt Server Cards for distribution; add single-cell atlases (CELLxGENE/HCA) into the fetch-breadth wave when scoped.
5. **Separate track:** non-research sibling server (own spec, own cycle).
