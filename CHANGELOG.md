# Changelog

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
