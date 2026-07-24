# Why data-aggregator-mcp

Most MCP servers in the research space sit at one of two extremes. **Single-source
servers** wrap one repository well — DataCite, PubMed, ClinicalTrials.gov, a
preprint index — with no way to ask one question across archives, omics, and the
literature at once. **Breadth gateways** proxy hundreds of APIs as hundreds of raw
tools, but hand back each source's native payload: no shared model, no dedup, no
cross-source joins. And **deep-research agents** are tuned to find _papers_, then
frequently fabricate a download when the actual bytes aren't reachable.

This server occupies the middle that neither extreme covers: **a normalized,
multi-domain data layer.** One search fans out across research-data archives,
omics registries, and the literature; results come back as one `DataResource`
model, deduplicated by DOI; and `fetch` either verifies the bytes against a
checksum or fails loud — it never pretends.

## The shape

Six tools, one model:

- **`search`** — fan out across all sources (or a chosen subset), get back
  compact `DataResource` records, deduplicated by DOI. Can attach a whole-search
  Run Crate (`provenance_crate`) recording how the result set was produced.
- **`resolve`** — the full record for one id: file manifest, paper↔data links,
  normalized access/license, citation in any CSL style, open-access full text,
  trust signals, FAIR assessment, and optional Croissant / RO-Crate /
  provenance-dossier export.
- **`fetch`** — stream files to disk with checksum verification where the source
  exposes one, optional archive unpacking, and a fail-loud integrity sniff.
- **`operate`** — read the schema, preview rows, or run a read-only SQL `SELECT`
  against a remote Parquet/CSV/TSV **without downloading it** (Parquet footer +
  DuckDB httpfs range reads). Optional `[operate]` extra.
- **`relate`** — take a handful of resolved ids and report how they connect —
  shared accession, shared cross-identifier, explicit link, or version lineage —
  naming the literal shared value as evidence. Metadata hints only.
- **`list_sources`** — what's wired, each source's fetch guarantees, and an
  optional health probe.

Plus MCP **prompts** (`find_data`, `data_behind_paper`, `search_resolve_fetch`)
and MCP **resources** (`dataresource://catalog`, `dataresource://record/{id}`).

Sources (v0.41.1, 14 wired): **Zenodo**, **DataCite** (Dryad / Figshare /
Dataverse / OSF / OpenNeuro / Mendeley), **NCBI omics** (GEO / SRA / BioProject) +
**ENA**, **BioStudies** (EBI, incl. ArrayExpress),
**literature** (PubMed / OpenAIRE / EuropePMC / Unpaywall),
**HuggingFace** datasets, **DataONE** (eco/environmental federation,
checksum-verified fetch via Member Nodes), **OmicsDI** (proteomics / metabolomics,
with direct PRIDE / MetaboLights fetch), **DANDI** (neurophysiology),
**CZ CELLxGENE** (single-cell), **OpenML**, **RCSB PDB**, **UniProtKB**, and the
**GWAS Catalog**.

## What only this does

Six capabilities are uncontested across the MCP research ecosystem. Swept
2026-07-21 against the official MCP registry (`search=dataset`), Glama's
`research-and-data` category, and a web sweep of the bio/dataset MCP space. That
sweep is registry-scoped — it is not a claim about every server that exists.

1. **Normalized multi-domain unification + cross-source dedup.** Every source —
   archive, omics registry, or paper — lands in the same `DataResource` shape, and
   the same DOI from two sources collapses to one record, with the _fetchable_
   copy winning over bare metadata. A gateway that returns each API's raw payload
   structurally cannot do this.
2. **Taxonomy synonym query-expansion.** `organism="Orobanche aegyptiaca"` also
   matches records filed under `Phelipanche aegyptiaca` via NCBI Taxonomy — a
   species rename doesn't cost you results. Others filter by organism; none expand
   the query across synonyms.
3. **Bidirectional paper↔data bridge.** Resolve a paper and get links to the GEO /
   SRA / BioProject / DataCite records it produced; resolve a dataset and get back
   to its literature. Multi-repo and MCP-native.
4. **Verified-fetch-or-fail-loud.** Fetch verifies MD5 / SHA-256 where the source
   publishes one, and a content sniff rejects an HTML paywall page served as a
   "PDF". When bytes genuinely aren't reachable, it raises — it does not invent a
   path. This is the direct answer to deep-research tools that hallucinate on
   fetch-failure.
5. **FAIR assessment + research-object packaging.** `resolve` can attach an
   RDA-grounded FAIRness score (Maturity Model v0.90, with per-indicator RDA ids
   and actionable gaps), and export the record as Croissant, RO-Crate, or a
   `format="provenance"` dossier that composes version-currency, licence/SPDX,
   FAIRness, and retraction signals into one machine-readable artifact — with a
   whole-search Run Crate available on `search`. No other MCP server
   found in the sweep combines RO-Crate packaging, FAIR scoring, and
   checksum-verified fetch.
6. **Query the data without downloading it.** `operate` runs schema / preview /
   read-only SQL against remote columnar files via range reads — so an agent can
   decide whether a 40 GB table is worth fetching before it fetches it.

## Versus the alternatives

|                                                             | Multi-domain (archive+omics+lit) | Normalized model + DOI dedup | Taxonomy expansion | Paper↔data bridge |    Verified fetch     |   FAIR / RO-Crate   |
| ----------------------------------------------------------- | :------------------------------: | :--------------------------: | :----------------: | :---------------: | :-------------------: | :-----------------: |
| **data-aggregator-mcp**                                     |                ✅                |              ✅              |         ✅         |        ✅         |          ✅           |         ✅          |
| Single-source servers (datacite-, pubmed-, clinicaltrials-) |          ❌ one source           |             n/a              |         ❌         |        ❌         |        varies         |         ❌          |
| Breadth gateways (600+ sources)                             |           ✅ by count            |  ❌ raw per-source payloads  |         ❌         |        ❌         |          ❌           |         ❌          |
| Biomedical knowledgebase hubs (e.g. BioContextAI, 20+ DBs)  |        ✅ annotation DBs         |              ❌              |         ❌         |      partial      | ❌ answers, not bytes |         ❌          |
| ML-dataset / Croissant tools (e.g. Eclair)                  |         ❌ no omics/lit          |    ❌ not DOI-normalized     |         ❌         |        ❌         |       downloads       | partial (Croissant) |
| Multi-source bio (e.g. BioMCP)                              |     partial, clinical-biased     |              ❌              |         ❌         |      partial      |   ❌ dataset fetch    |         ❌          |

Competitor characterizations are from a 2026-07-21 ecosystem sweep; the named tools
are real and good at what they do — the table contrasts _axes_, not quality. In
particular, BioContextAI-class hubs are excellent at _answering biomedical
questions_ from annotation databases; they are not trying to hand you verified
bytes, which is what this server is for.

## Where the frontier is (honest gaps)

This is a discovery-and-fetch layer, not the whole stack. Today it does **not**:

- **Ship a managed, authenticated deployment.** Streamable HTTP now works
  (`--transport http`, with DNS-rebinding protection on by default), so a remote
  MCP client can reach it — but you host it yourself. There is no OAuth/token
  layer, no multi-tenancy, and `fetch(dest=…)` writes to the _server's_ disk, so
  a shared instance is not yet a safe default.
- **Support the MCP Tasks extension.** A multi-GB `fetch` blocks the tool call
  rather than running as a long-lived task. Tracking the 2026-07-28 spec revision.
- **Reason semantically at index scale.** `rank=semantic` re-ranks an
  already-fetched keyword window against a remote embeddings endpoint; it is not
  embedding recall over a full index.
- **Cover several major repositories.** ClinicalTrials.gov, GDC/TCGA, ENCODE, and
  Ensembl are not wired. Outside the life sciences, Kaggle, data.gov, NASA
  Earthdata/CMR, and GBIF are absent — the `DataResource` model is domain-general,
  but the wiring is currently ~90% biological.
- **Elicit clarification on a free-text query.** An ambiguous `query` string is
  expanded and searched, never queried back to the user. Elicitation is wired only
  for the narrower case where an explicit ontology param (`organism`, `tissue`, …)
  matches no term in its registry: a form-capable client is asked for a replacement,
  and every client gets the `unresolved[]` echo either way. Terms that resolve are
  not second-guessed — a live probe found the top-hit heuristic already picks
  correctly on the rare genuinely-ambiguous term.

For citation-graph _traversal_, pair it with an OpenAlex MCP — that boundary is
deliberate, not an oversight.

## Who it's for

Agents and pipelines that need to **find the right dataset across domains and
actually get the bytes** — with the cross-source dedup, taxonomy reach, and
paper↔data links that a normalized model makes possible, and the integrity,
FAIR, and provenance guarantees that make an automated fetch safe to trust.

```bash
uvx data-aggregator-mcp
```
