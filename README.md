# 🔎 data-aggregator-mcp

**One MCP server to find and fetch research data across archives, omics
registries, and literature — behind a single normalized model.**

[![PyPI](https://img.shields.io/pypi/v/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/musharna/data-aggregator-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/musharna/data-aggregator-mcp/actions/workflows/ci.yml)
[![Glama](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp)

`search` one query across **Zenodo, DataCite** (Dryad / Figshare / Dataverse /
OSF / Mendeley), **NCBI omics** (GEO / SRA / BioProject), **DataONE** (eco /
environmental), **literature** (PubMed / OpenAIRE), **OmicsDI** (proteomics /
metabolomics), and **HuggingFace** datasets — deduplicated, normalized, and
cross-linked. `resolve` any hit to its file manifest, citation, trust signals,
and the data it points at. `fetch` it to disk with checksum verification.

mcp-name: io.github.musharna/data-aggregator-mcp

<p align="center">
  <img src="examples/assets/demo.svg"
       alt="data-aggregator-mcp stdio demo — initialize, tools/list (search, resolve, fetch, operate, relate, list_sources), and a live list_sources call showing the wired sources across archives, omics, and literature"
       width="820">
</p>

## ✨ Why this

Most data MCPs wrap a single source. This one **unifies** them behind six tools
and one `DataResource` model, so an agent searches once and gets back comparable
records:

- **Multi-domain, one model** — generalist archives + raw omics + literature,
  deduplicated by DOI (the fetchable record wins over bare metadata).
- **Taxonomy synonym expansion** — `organism="Orobanche aegyptiaca"` also matches
  `Phelipanche aegyptiaca` (NCBI Taxonomy), so a species rename doesn't cost you
  results.
- **Paper → data bridge** — resolve a paper and get links to the GEO / SRA /
  BioProject / DataCite records it produced.
- **Verified fetch** — streams to disk with md5 verification where the source
  exposes a checksum, optional archive unpacking, and a fail-loud integrity
  sniff that rejects an HTML paywall page served as a "PDF".
- **Citations, access & full text** — render a citation in any CSL style, get
  normalized access/license, and pull open-access full text — all in one
  `resolve`.
- **Trust signals** — usage `metrics` (citations / views / downloads / likes),
  version status (`is_latest` / `superseded_by`), and `last_updated` freshness,
  surfaced wherever the source exposes them.
- **Interop exports** — `resolve(format="croissant")` or `"ro-crate"` hands a
  dataset to an ML or research-packaging pipeline as standard JSON-LD.
- **Operate on data in place** — `operate` reads the schema, previews rows, or
  runs a read-only SQL `SELECT` against a remote Parquet/CSV/TSV **without
  downloading it** (Parquet footer + DuckDB httpfs range reads). Optional
  `[operate]` extra; base install is unchanged.
- **Relate across records** — `relate` takes a handful of resolved ids and
  reports how they connect — shared accession, shared cross-identifier, an
  explicit link, or version lineage — naming the literal shared value as
  evidence. Metadata hints only: it never reads files or executes a join.

→ Full rationale and a comparison vs. single-source servers, breadth gateways, and
ML-dataset tools: **[docs/POSITIONING.md](docs/POSITIONING.md)**.

## ⚡ Quickstart

Run with no install:

```bash
uvx data-aggregator-mcp
```

Register with Claude Code:

```bash
claude mcp add data-aggregator -- uvx data-aggregator-mcp
```

A typical agent flow:

```text
search("drought stress RNA-seq", organism="Sorghum bicolor")
  → [ geo:GSE..., sra:SRX..., zenodo:..., pubmed:... ]   # deduped, taxa-normalized

resolve("sra:SRX079566")
  → DataResource{ files: [ENA FASTQ urls…], access: "open", taxa: [...] }

fetch("sra:SRX079566", dest="./data")
  → ["./data/SRX079566_1.fastq.gz", …]                   # md5-verified
```

<details>
<summary>Other ways to run (pip, python -m, raw client config)</summary>

```bash
pip install data-aggregator-mcp
data-aggregator-mcp        # or: python -m data_aggregator_mcp
```

To use the `operate` tool (query remote tabular files in place), install the
optional extra:

```bash
pip install "data-aggregator-mcp[operate]"
```

Add to a client's MCP config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "data-aggregator": {
      "command": "uvx",
      "args": ["data-aggregator-mcp"],
      "env": { "NCBI_API_KEY": "your-optional-key" }
    }
  }
}
```

</details>

## 🗂️ Sources

| Source                       | Discover |       Fetch       |     Checksum     |
| ---------------------------- | :------: | :---------------: | :--------------: |
| Zenodo                       |    ✅    |        ✅         |       md5        |
| DataCite → Figshare          |    ✅    |        ✅         |       md5        |
| DataCite → Dataverse         |    ✅    |        ✅         |       md5        |
| DataCite → OSF               |    ✅    |        ✅         |       md5        |
| DataCite → Dryad             |    ✅    |  manifest only¹   | sha-256 (listed) |
| DataCite → Mendeley & others |    ✅    |         —         |        —         |
| NCBI SRA                     |    ✅    |  ✅ (ENA FASTQ)   |       md5        |
| NCBI GEO                     |    ✅    |   ✅ (`suppl/`)   |      none²       |
| NCBI BioProject              |    ✅    |    → SRA links    |        —         |
| PubMed / OpenAIRE            |    ✅    | ✅ (OA full text) |      none²       |
| HuggingFace datasets         |    ✅    | ✅ (resolve URL)  |       none       |
| DataONE (eco/env)            |    ✅    | ✅ (Member Node)  |  md5 / sha-256   |
| OmicsDI → PRIDE              |    ✅    |  ✅ (HTTPS FTP)   |    size only     |
| OmicsDI → MetaboLights       |    ✅    |  ✅ (HTTPS FTP)   |       none       |
| OmicsDI → other MS repos     |    ✅    |         —         |        —         |

¹ Dryad downloads are token / bot-challenge gated, so `fetch` fails loud;
`resolve` still lists the files.
² No upstream checksum — `fetch` verifies content-type instead (rejects an HTML
page served in place of a binary).

## 🛠️ Tools

### `search(query?, size?, sources?, organism?, kind?, published_after?, published_before?, rank?, cursor?)`

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
  `software`.
- `published_after` / `published_before` — filter by publication year.
- `rank` — `relevance` (default) or `semantic` (re-rank the fetched page by
  embedding similarity to the query; needs `EMBEDDING_API_BASE`, degrades to
  relevance order otherwise).
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
  harness below). On our small verified set `multi_query=` is the stronger,
  always-safe recall lever; `understand=` is approximately neutral with a weak
  local model.**
- `multi_query` — opt into diverse multi-query recall expansion (default false).
  An LLM generates up to a few deliberately-diverse reformulations of your query
  (different facets/synonyms/framings, not paraphrases), each is fanned out across
  every source, and the deduped union is re-ranked against your **original** query —
  surfacing relevant records a single keyword query would miss. Bounded at
  `MAX_QUERY_VARIANTS` (4, incl. the original, which is always kept so recall never
  drops below baseline), so it costs at most N× the upstream calls. Composes with
  `understand=` (which structures variant 0). The variants used are echoed in
  `query_expansion`. Needs an LLM endpoint (`LLM_API_BASE`); with none configured
  the search runs as a normal single query and notes it in `errors['multi_query']`.
- `cursor` — opaque token from a prior result's `next_cursor`; pages forward
  across every source. In `cursor` mode the other params are read from the
  token, so `query` is optional.

### `resolve(id, cite?, format?)`

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
- **`format`** — pass `format="croissant"` (file-level Croissant JSON-LD) or
  `"ro-crate"` (minimal RO-Crate 1.1) to attach a standard manifest under the
  matching field, for ML or research-packaging pipelines.

### `fetch(id, dest?, files?, max_bytes?, force?, extract?)`

Download files to disk and return their paths. Streams under a `max_bytes` guard
(`force` to override) with md5 verification wherever a checksum exists.

- `files` — restrict to a subset of the resolved manifest.
- `extract` — unpack downloaded zip / tar archives in place, guarded against
  path traversal and runaway extracted size. Off by default.
- Unverified fetches (GEO `suppl/`, literature full text) get a content-type
  sniff that fails loud if a declared binary is actually an HTML page.
- Fetchable: **Zenodo**, **SRA**, **GEO**, **DataONE** (Member-Node objects,
  md5/sha-256 verified), DataCite-hosted **Figshare** / **Dataverse** / **OSF**,
  **HuggingFace** datasets, **PRIDE** / **MetaboLights** (via OmicsDI, unverified),
  and **literature** open-access full text. **Dryad**, other DataCite repos, and
  other OmicsDI repos (MassIVE / GNPS / ...) are discovery-only and raise
  `FetchNotSupportedError`.

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

Backed by the Parquet footer reader + DuckDB `httpfs` range reads. `sql` runs in
a locked-down DuckDB (read-only, local filesystem disabled, single-SELECT
validation, row / wall-clock caps). Requires the optional `[operate]` extra
(`pip install data-aggregator-mcp[operate]`); without it, `operate` returns a
clear install-the-extra message and the other four tools are unaffected.

Any HuggingFace dataset with a datasets-server converted view is operable
(`schema` / `preview` / `head` / `sql`): `resolve` surfaces the auto-converted
Parquet files (`source="hf-datasets-server"`) even for datasets stored as
JSON/JSONL/arrow, so pass `file=<config>/<split>/...parquet` to pick a split when
there are several.

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

## ⚙️ Configuration

Both optional, set via environment variables:

- `NCBI_API_KEY` — raises the NCBI E-utilities rate limit (3 → 10 req/s) used by
  the omics, literature, and taxonomy lookups.
- `UNPAYWALL_EMAIL` — enables the Unpaywall fallback leg of literature full-text
  retrieval (the EuropePMC leg works without it).
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

## 🧪 Develop

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest -q
uv run ruff check src tests
DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -k live -q   # real-API probes
```

The README demo (`examples/assets/demo.svg`) is recorded network-free from
`examples/_demo_stdio.py` — see the header of that file to re-record.

## License

MIT — see [LICENSE](LICENSE).
