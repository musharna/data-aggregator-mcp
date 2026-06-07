# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
