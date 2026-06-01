# Why data-aggregator-mcp

Most MCP servers in the research space sit at one of two extremes. **Single-source
servers** wrap one repository well — DataCite, PubMed, a preprint index — with no
way to ask one question across archives, omics, and the literature at once.
**Breadth gateways** proxy hundreds of APIs as hundreds of raw tools, but hand
back each source's native payload: no shared model, no dedup, no cross-source
joins. And **deep-research agents** are tuned to find _papers_, then frequently
fabricate a download when the actual bytes aren't reachable.

This server occupies the middle that neither extreme covers: **a normalized,
multi-domain data layer.** One search fans out across research-data archives,
omics registries, and the literature; results come back as one `DataResource`
model, deduplicated by DOI; and `fetch` either verifies the bytes against a
checksum or fails loud — it never pretends.

## The shape

Four tools, one model:

- **`search`** — fan out across all sources (or a chosen subset), get back
  compact `DataResource` records, deduplicated by DOI.
- **`resolve`** — the full record for one id: file manifest, paper↔data links,
  normalized access/license, citation in any CSL style, open-access full text,
  trust signals, and optional Croissant / RO-Crate export.
- **`fetch`** — stream files to disk with checksum verification where the source
  exposes one, optional archive unpacking, and a fail-loud integrity sniff.
- **`list_sources`** — what's wired, each source's fetch guarantees, and an
  optional health probe.

Sources (v0.18.0): **Zenodo**, **DataCite** (Dryad / Figshare / Dataverse / OSF /
Mendeley), **NCBI omics** (GEO / SRA / BioProject) + **ENA**, **PubMed** +
**OpenAIRE**, **HuggingFace** datasets, **DataONE** (eco/environmental federation,
checksum-verified fetch via Member Nodes), and **OmicsDI** (proteomics /
metabolomics, with direct PRIDE / MetaboLights fetch).

## What only this does

Four capabilities are uncontested across the MCP research ecosystem (registry +
Glama + Smithery + PulseMCP + biocontext.ai + GitHub, swept 2026-05):

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

## Versus the alternatives

|                                                              | Multi-domain (archive+omics+lit) | Normalized model + DOI dedup | Taxonomy expansion | Paper↔data bridge |  Verified fetch  |
| ------------------------------------------------------------ | :------------------------------: | :--------------------------: | :----------------: | :---------------: | :--------------: |
| **data-aggregator-mcp**                                      |                ✅                |              ✅              |         ✅         |        ✅         |        ✅        |
| Single-source servers (datacite-, pubmed-, paper-search-mcp) |          ❌ one source           |             n/a              |         ❌         |        ❌         |      varies      |
| Breadth gateways (e.g. Pipeworx, 600+ sources)               |           ✅ by count            |  ❌ raw per-source payloads  |         ❌         |        ❌         |        ❌        |
| ML-dataset / Croissant tools (e.g. Eclair, 700k datasets)    |         ❌ no omics/lit          |    ❌ not DOI-normalized     |         ❌         |        ❌         |    downloads     |
| Semantic / KG (e.g. EOSC data-commons)                       |        ❌ single-commons         |              ❌              |         ❌         |        ❌         |     metadata     |
| Multi-source bio (e.g. BioMCP)                               |     partial, clinical-biased     |              ❌              |         ❌         |      partial      | ❌ dataset fetch |

Competitor characterizations are from a 2026-05-31 ecosystem sweep; the named tools
are real and good at what they do — the table contrasts _axes_, not quality.

## Where the frontier is (honest gaps)

This is a discovery-and-fetch layer, not the whole stack. Today it does **not**:

- **Reason semantically.** `rank=semantic` re-ranks an already-fetched keyword
  window; it is not embedding recall over a full index. For NL→knowledge-graph
  retrieval, EOSC data-commons and Eclair lead.
- **Operate on the data in place.** It fetches files; it does not yet preview /
  slice / SQL them (a DuckDB-style `operate` verb is planned).
- **Run as a hosted endpoint.** It ships stdio-only via `uvx` / PyPI; remote HTTP
  transport is deferred.
- **Expose MCP resources / prompts.** Four tools today, no `://` resource URIs.

For citation-graph _traversal_, pair it with an OpenAlex MCP — that boundary is
deliberate, not an oversight.

## Who it's for

Agents and pipelines that need to **find the right dataset across domains and
actually get the bytes** — with the cross-source dedup, taxonomy reach, and
paper↔data links that a normalized model makes possible, and the integrity
guarantees that make an automated fetch safe to trust.

```bash
uvx data-aggregator-mcp
```
