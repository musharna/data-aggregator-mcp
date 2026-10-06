# Reference

Every tool and parameter, the HTTP transport, and the environment variables.
The [README](https://github.com/musharna/data-aggregator-mcp/blob/main/README.md) covers install and what the server is for.

## Tools

### `search(query?, size?, sources?, organism?, disease?, tissue?, chemical?, assay?, kind?, published_after?, published_before?, rank?, cursor?, collapse_mirrors?, understand?, multi_query?, provenance?)`

Fan out across all wired sources in parallel and return compact `DataResource`
records, deduped by DOI. Per-source failures land in `errors{}` — never silently
dropped.

- `organism` — expand the query with NCBI-Taxonomy synonyms; the expansion is
  echoed in `taxon_expansion`, and results carry normalized `taxa[]`
  (`{taxid, name}`) plus a `described_in` link to plant-genomics-mcp for plant
  taxa.
- `sources` — restrict the fan-out, e.g. `["omics"]`.
- `size` — max results (1–50).
- `kind` — keep only `dataset` / `sequencing_run` / `study` / `publication` /
  `software`. A record whose upstream type none of these covers (a Zenodo image, a
  DataCite `Audiovisual`, an untyped record) is kind `other` and matches no filter.
- `published_after` / `published_before` — filter by publication year.
- `rank` — `relevance` (default) or `semantic`. `relevance` puts hits that name
  more of the search first: each facet you set (any of its expanded names), then
  each word of the query, found in the title, description, subjects or organism.
  Hits that name as much stay in the sources' own order, taken in turn, and
  nothing is dropped. `semantic` re-ranks the fetched page by embedding
  similarity to the query; it needs `EMBEDDING_API_BASE` and falls back to
  `relevance` order otherwise.
- `understand` — opt into LLM query understanding (default false). A free-text
  query is **normalized** into a focused keyword query: conversational fluff
  (`"I'm looking for…"`, `"where can I find…"`) is stripped while the scientific
  and entity terms are kept so they still match by text. The LLM also detects
  structured entities (organism/disease/tissue/chemical/assay, kind) — these are
  **echoed in `query_understanding.extracted` for transparency but not
  auto-applied**, because ANDing LLM-_inferred_ facets across free-text keyword
  upstreams over-constrains and hurts recall. Only the cleaned `keyword_core` and
  explicit `year` scopes are applied; the ontology resolvers still run on the
  facets **you** pass (the LLM proposes, you dispose). Needs an LLM endpoint
  (`LLM_API_BASE`); with none configured the search runs unchanged and notes it in
  `errors['understand']`. **Effectiveness is query- and model-dependent — opt-in /
  default-off; validate the recall lift on your own corpus and LLM (see the eval
  harness below).** `understand=` was measured once (v0.38.0, 2026-06-11) on a
  5-query verified gold set: mean recall@20 lift −0.10 against the plain query,
  with 4 of the 5 queries neutral or better. `multi_query=` has not been measured.
- `multi_query` — opt into diverse multi-query recall expansion (default false).
  An LLM generates up to a few deliberately-diverse reformulations of your query
  (different facets/synonyms/framings, not paraphrases), each is fanned out across
  every source, and the deduped union is re-ranked against your **original** query,
  aiming to reach records a single keyword query would miss. Bounded at
  `MAX_QUERY_VARIANTS` (4, incl. the original), so it costs at most N× the upstream
  calls. The original query's results are always among the candidates, but only the
  top `size` of the re-ranked union are returned, so a result the plain query would
  have returned can be displaced by one from a variant. Composes with
  `understand=` (which structures variant 0). The variants used are echoed in
  `query_expansion`. Needs an LLM endpoint (`LLM_API_BASE`); with none configured
  the search runs as a normal single query and notes it in `errors['multi_query']`.
- `cursor` — opaque token from a prior result's `next_cursor`; pages forward
  across every source. In `cursor` mode the other params are read from the
  token, so `query` is optional.

### `resolve(id, cite?, format?, trust?, fair?, use?)`

Full record + files manifest. Routes by id shape — `zenodo:7654321`, a bare DOI,
`datacite:10.5061/dryad.x`, an omics id (`sra:SRX079566`, `geo:GSE332789`,
`bioproject:PRJNA1468572`), a literature id (`pubmed:34320281`, `openaire:<id>`),
a HuggingFace id (`hf:owner/name`), a DataONE id (`dataone:doi:10.5063/F1HT2M7Q`),
or an OmicsDI id (`omicsdi:pride:PXD000001`). Attaches, where available:

- **`files[]`** — ENA FASTQ manifest (SRA), GEO `suppl/`, or the host repo's
  native manifest (Figshare / Dataverse / OSF / Dryad).
- **`links[]`** — paper → data: `pubmed:` → `sra:` / `geo:` / `bioproject:` (NCBI
  elink); `openaire:` → `datacite:` (ScholeXplorer Scholix).
- **`access` / `license`** — normalized status
  (`open` / `embargoed` / `restricted` / `closed` / `unknown`) and license where
  the source exposes it.
- **`identifiers`** — normalized `{pmid, pmcid, doi}`, plus an open-access
  full-text `FileEntry` (EuropePMC XML, or an Unpaywall PDF fallback) for papers.
- **`citation`** — pass `cite=<format>`: `bibtex`, `ris`, `csl-json`, or any CSL
  style name (`apa`, `mla`, `vancouver`, …). DOI records use content
  negotiation; others render CSL-JSON from metadata. Off by default; failures
  degrade quietly.
- **trust signals** — `metrics` (citations / views / downloads / likes),
  `is_latest` / `superseded_by` (derived from version links), and `last_updated`
  freshness, where the source provides them.
- **`errors`** — `{step: message}` when an enrichment step failed on this record
  (e.g. `taxonomy` during an NCBI rate limit); the rest of the record stands. Such a
  record is not cached, so the next resolve retries the step.
- **`truncated`** — `{field: note}` when a list on this record is deliberately partial,
  e.g. a BioProject's `links` past 100 SRA runs: `first 100 of 891 SRA runs; …`. Empty
  when every list is complete.
- **`trust=true`** — attach retraction status (via Crossref) under `trust{}`.
  One extra Crossref call; meaningful for DOI-bearing records only.
- **`fair=true`** — attach an RDA-grounded FAIRness score (0–100 + F/A/I/R
  sub-scores + actionable gaps) computed from the record metadata under `fair{}`.
  Pure/local — no extra network call.
- **`use=<intent>`** — attach a licence-compatibility advisory under
  `license_compat{}` for the intended use (`commercial` / `redistribute` /
  `modify` / `ml-training`). Returns ALLOW/REVIEW/DENY with the governing clause.
  Metadata-derived advisory, **not legal advice**; an absent/unrecognized licence
  yields REVIEW.
- **`format`** — pass `format="croissant"` (file-level Croissant JSON-LD),
  `"ro-crate"` (minimal RO-Crate 1.1), or `"provenance"` (one-call RO-Crate 1.1
  data-availability dossier bundling version-currency, licence+SPDX, FAIR score,
  and retraction status) to attach a standard manifest under the matching field.
  The crates pass rocrate-validator's RO-Crate 1.1 profile; the assessment fields
  schema.org lacks are defined in [docs/vocab.md](vocab.md).

### `fetch(id, dest?, files?, max_bytes?, force?, extract?)`

Download files to disk and return their paths. Streams under a `max_bytes` guard
(`force` to override) with md5 / sha-256 verification wherever the source
publishes a checksum.

- `files` — restrict to a subset of the resolved manifest.
- `extract` — unpack downloaded zip / tar archives in place, guarded against
  path traversal and runaway extracted size. Off by default.
- Sources without a checksum are downloaded unverified. The one content check
  there is an HTML sniff on files declared as PDF or XML (literature full text,
  some data.gov distributions): it fails loud if the body is actually an HTML page.
- Checksum-verified: **Zenodo**, **SRA** (ENA FASTQ), **DataONE** (Member-Node
  objects), DataCite-hosted **Figshare** / **Dataverse** / **OSF**, **OpenML**
  (ARFF), **MetaboLights** (via OmicsDI; sha-256 from the study's `HASHES/`) and
  **DANDI** (sha-256; an asset whose hash DANDI has not computed yet is unverified).
- Fetchable but unverified: **GEO** `suppl/`, **HuggingFace** datasets,
  **PRIDE** (via OmicsDI), DataCite-hosted **OpenNeuro**,
  **CZ CELLxGENE**, **RCSB PDB**, **UniProtKB**, **BioStudies**,
  **GBIF** (Darwin Core Archives), **data.gov** distributions, and **literature**
  open-access full text.
- **Dryad**, other DataCite repos, other OmicsDI repos (MassIVE / GNPS / ...),
  **BioProject**, **NASA CMR**, the **GWAS Catalog** and **NGDC** are discovery-only and
  raise `FetchNotSupportedError`.

### `list_sources()`

Wired sources with their capabilities — layer, kinds, supported filters,
fetchability, `operable` flag, id examples, auth, and rate limits.

### `operate(op, id, file?, query?, n?, columns?)`

Inspect or query a remote tabular file (Parquet / CSV / TSV) **without
downloading it**. Addresses a file by catalog `id` + `file` name (defaults to the
first tabular file on the resolved record). Ops:

- `schema` — column names + types (reads the Parquet footer / sniffs the CSV
  header; no full load).
- `preview` — a small sample of rows.
- `head` — the first `n` rows (default 20), optionally restricted to `columns`.
- `sql` — a read-only `SELECT` (the file is the view `data`), e.g.
  `SELECT col, count(*) FROM data GROUP BY 1`.
- `peek` — per-column profile via DuckDB `SUMMARIZE` (type, null-rate,
  approximate distinct count, min/max, numeric quartiles) **without
  downloading** the file. Like `head`/`sql`, reads the whole file and honors
  the source-size ceiling.

Backed by the Parquet footer reader + DuckDB `httpfs` range reads. `sql` runs in
a locked-down DuckDB (read-only, local filesystem disabled, single-SELECT
validation, row / wall-clock caps). Requires the optional `[operate]` extra
(`pip install data-aggregator-mcp[operate]`); without it, `operate` returns a
clear install-the-extra message and the other five tools are unaffected.

Any HuggingFace dataset with a datasets-server converted view is operable
(`schema` / `preview` / `head` / `sql`): `resolve` surfaces the auto-converted
Parquet files (`source="hf-datasets-server"`) even for datasets stored as
JSON/JSONL/arrow, so pass `file=<config>/<split>/...parquet` to pick a split when
there are several. A split HF converted only in part (its first 5 GB) is named
`<config>/partial-<split>/...`, so a query on it covers that part, not the whole split.

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

### Prompts

Three workflow prompts surface in clients (e.g. `/mcp__data_aggregator__*` in
Claude Code):

- **`find_data`** — find datasets for a topic, optionally scoped to an organism.
- **`data_behind_paper`** — find the datasets / accessions behind a paper.
- **`search_resolve_fetch`** — walk the end-to-end search → resolve → fetch flow.

## Transports

**stdio (default)** — the server runs as a child of the client, so `fetch()`
writes to your own disk. Nothing to configure; every command above uses it.

**Streamable HTTP** — the same six tools, prompts, and resources over HTTP:

```bash
data-aggregator-mcp --transport http     # → http://127.0.0.1:8000/mcp/
```

| flag                       | default          | notes                                                             |
| -------------------------- | ---------------- | ----------------------------------------------------------------- |
| `--transport {stdio,http}` | `stdio`          |                                                                   |
| `--host`                   | `127.0.0.1`      | this machine only; any non-loopback value requires `--allow-host` |
| `--port`                   | `8000`           |                                                                   |
| `--allow-host HOST:PORT`   | auto on loopback | permitted `Host` header, repeatable — **required off loopback**   |
| `--allow-origin ORIGIN`    | derived          | permitted browser `Origin` header, repeatable                     |
| `--stateless`              | off              | fresh transport per request, no session affinity                  |
| `--json-response`          | off              | plain JSON responses instead of SSE streams                       |

The endpoint is served at **`/mcp/`** — with the trailing slash. `/mcp` answers
`307` redirecting there, which is fine for any client that follows redirects (a
`307` preserves the POST body); point one that doesn't straight at `/mcp/`. In
stateful mode, sessions idle for 30 minutes are reaped.

**DNS-rebinding protection is always on.** A loopback bind derives its own
host/origin allowlist, so the default needs no configuration. A non-loopback bind
(`--host 0.0.0.0`, a LAN address, a container interface) **refuses to start**
without at least one explicit `--allow-host` — guessing an allowlist there is
precisely the hole the protection exists to close, so it fails loud instead of
open:

```bash
data-aggregator-mcp --transport http --host 0.0.0.0 \
  --allow-host data.example.org:8000
```

Once running, a request whose `Host` header is outside the allowlist is refused
with `421 Invalid Host header`.

> ⚠️ **`fetch(dest=…)` writes to the _server's_ filesystem, not the client's.**
> Over stdio those are the same disk; over HTTP they may be different machines,
> and the caller gets back paths it cannot read. Treat `dest` on an HTTP
> deployment as server-side staging, or use stdio when you need the bytes
> locally. `search`, `resolve`, `operate`, `relate`, and `list_sources` are
> unaffected — they return data, not paths.

## Configuration

All optional, set via environment variables:

- `NCBI_API_KEY` — raises the NCBI E-utilities rate limit (3 → 10 req/s) used by
  the omics, literature, and taxonomy lookups.
- `DATA_GOV_API_KEY` — optional; data.gov works without it through the keyless
  catalog API (`catalog.data.gov`). With a free
  [api.data.gov](https://api.data.gov/signup/) key set, data.gov requests go
  through the api.data.gov gateway instead (1,000 requests/hour per key).
- `UNPAYWALL_EMAIL` — enables the Unpaywall fallback leg of literature full-text
  retrieval (the EuropePMC leg works without it).
- `NCBI_EMAIL` — contact address sent to NCBI's ID converter; falls back to
  `UNPAYWALL_EMAIL` when unset.
- `DATAVERSE_BASE_URL` — resolve Dataverse DOIs against a different installation
  (default `https://dataverse.harvard.edu`).
- `CACHE_TTL_SECONDS` — resolve-cache lifetime in seconds (default `3600`; an
  unparseable value falls back to that default).
- `EMBEDDING_API_BASE` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` — an
  OpenAI-compatible embeddings endpoint enabling `rank=semantic`. Absent ⇒
  semantic re-rank degrades to relevance order. Key is optional (keyless local
  servers supported); model defaults to `text-embedding-3-small`.
- `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` — an OpenAI-compatible
  `/chat/completions` endpoint enabling `search(understand=true)` (NL→structured
  query rewriting) **and** `search(multi_query=true)` (diverse multi-query recall
  expansion). Absent ⇒ both run the raw query unchanged and note it in
  `errors['understand']` / `errors['multi_query']`. Key is optional (keyless local
  servers supported); model defaults to `gpt-4o-mini` (a passthrough string — set
  it to whatever your endpoint serves). `multi_query` fans out at most
  `MAX_QUERY_VARIANTS` (4, incl. the original) variants, bounding the N× cost.

To measure the recall lift of `understand=true` / `multi_query=true` on a small
labeled set, run the gated eval harnesses (need a live LLM endpoint):

```bash
DATA_AGGREGATOR_MCP_LIVE=1 LLM_API_BASE=... python scripts/eval_understand.py
DATA_AGGREGATOR_MCP_LIVE=1 LLM_API_BASE=... python scripts/eval_multi_query.py
```

They print per-query and mean recall@20 (understand / multi-query off vs. on). See
the fixtures at `scripts/eval_understand_fixture.json` and
`scripts/eval_multi_query_fixture.json`.
