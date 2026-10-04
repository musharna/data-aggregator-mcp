# data-aggregator-mcp

Search 17 research-data sources at once (data archives, omics repositories and
papers) and get one list back. Organism, disease and tissue names are expanded
with their synonyms in the 11 sources that accept them, records that share a DOI are collapsed to one, and downloads
are checked against the source's checksum where it publishes one.

[![PyPI](https://img.shields.io/pypi/v/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/musharna/data-aggregator-mcp/blob/main/LICENSE)
[![CI](https://github.com/musharna/data-aggregator-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/musharna/data-aggregator-mcp/actions/workflows/ci.yml)
[![Glama](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21636332.svg)](https://doi.org/10.5281/zenodo.21636332)

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install-0098FF?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=data-aggregator&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22data-aggregator-mcp%22%5D%7D)
[![Install in Cursor](https://img.shields.io/badge/Cursor-Install-000000?logo=cursor&logoColor=white)](https://cursor.com/install-mcp?name=data-aggregator&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkYXRhLWFnZ3JlZ2F0b3ItbWNwIl19)

mcp-name: io.github.musharna/data-aggregator-mcp

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/data-aggregator-mcp/main/examples/assets/demo.svg"
       alt="Live calls over stdio: a search for transcriptome data on Orobanche aegyptiaca that NCBI Taxonomy expands to Phelipanche aegyptiaca and that returns hits from SRA, BioProject, PubMed, OpenAIRE and DataCite; resolving one SRA hit to two FASTQ files with sizes and md5 checksums; fetching an OpenML ARFF file with its md5 verified; and SQL over a remote CSV on Hugging Face without downloading it"
       width="820">
</p>

## Install

Claude Code:

```bash
claude mcp add data-aggregator -- uvx data-aggregator-mcp
```

VS Code and Cursor: the buttons above. Any other MCP client: run
`uvx data-aggregator-mcp` as a stdio server.

<details>
<summary>Claude Desktop, pip, and the <code>operate</code> extra</summary>

Claude Desktop (`claude_desktop_config.json`) and most other clients:

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

With pip:

```bash
pip install data-aggregator-mcp
data-aggregator-mcp        # or: python -m data_aggregator_mcp
```

`operate` (SQL and previews over remote files) needs an optional extra:
`pip install "data-aggregator-mcp[operate]"`.

</details>

## What you can ask

**"Find transcriptomes for Orobanche aegyptiaca."** The species has been renamed.
NCBI Taxonomy maps it to *Phelipanche aegyptiaca*, and the sources that accept
synonyms match both names (the demo above).

**"What data came out of this paper?"**

```text
resolve("pubmed:40098680")
  links  geo:GSE284240, bioproject:PRJNA1198054, and 18 sra: experiments
  files  PMC11910882.xml   (open-access full text)
```

**"Can I train a model on this dataset?"**

```text
resolve("hf:scikit-learn/iris", use="ml-training")
  license_compat  ALLOW: "CC0-1.0 grants the permission(s) required for
                  ml-training: commercial-use, modifications"
```

The verdict is read from the record's licence metadata. It is advice, not a legal
opinion.

`resolve` also renders citations in any CSL style, checks Crossref for
retractions, scores FAIRness, and exports Croissant or RO-Crate. `relate` reports
how a set of records connect (a shared accession, DOI or explicit link).

## How a search runs

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/data-aggregator-mcp/main/docs/assets/architecture.svg"
       alt="How one search runs: NCBI Taxonomy maps Orobanche aegyptiaca to its current name, Phelipanche aegyptiaca, and the sources that accept synonyms search both names; the query goes to 17 sources in parallel (10 archives, 6 omics, 1 literature); records that share a DOI become one, keeping the copy that can be downloaded; a record can then be resolved to files and checksums, fetched with the checksum verified where the source publishes one, or queried with SQL without downloading it"
       width="820">
</p>

A source that fails is named in the result's `errors`, never dropped silently.

## Compared with other servers

| Server | Searches | One search | Checksum | Remote SQL | Licence check |
| --- | --- | :---: | :---: | :---: | :---: |
| **data-aggregator-mcp** | 17 sources: data archives, omics, papers | yes, DOI dedup | where published | yes | yes |
| [Mobus](https://github.com/mobus-ai/Mobus) | 20 general and ML data platforms | yes | — | row preview | yes |
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | papers: arXiv, PubMed, OpenAlex, Crossref and more | yes, deduplicated | — | — | — |
| [ToolUniverse](https://github.com/mims-harvard/ToolUniverse) | 1000+ tools: models, datasets, APIs, packages | papers | — | — | — |
| [BioMCP](https://github.com/genomoncology/biomcp) | genes, variants, trials, drugs, proteins, papers | yes (`search all`) | — | — | — |

— means the project's README doesn't describe it (READMEs read 2026-10-04).
Where they are ahead: BioMCP reaches clinical trials, ChEMBL and AlphaFold, which
this server doesn't; ToolUniverse covers far more ground overall; paper-search-mcp
covers far more literature sources; Mobus also compares datasets and checks schema
compatibility. More detail: [docs/POSITIONING.md](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/POSITIONING.md).

## Sources

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
| PubMed / OpenAIRE            |    ✅    | ✅ (OA full text) |      none³       |
| Hugging Face datasets        |    ✅    | ✅ (resolve URL)  |      none²       |
| DataONE (eco/env)            |    ✅    | ✅ (Member Node)  |  md5 / sha-256   |
| OmicsDI → PRIDE              |    ✅    |  ✅ (HTTPS FTP)   |      none²       |
| OmicsDI → MetaboLights       |    ✅    |  ✅ (HTTPS FTP)   |     sha-256      |
| OmicsDI → other MS repos     |    ✅    |         —         |        —         |
| DataCite → OpenNeuro         |    ✅    |   ✅ (snapshot)   |      none²       |
| DANDI (neurophysiology)      |    ✅    |    ✅ (302→S3)    |     sha-256      |
| CZ CELLxGENE (single-cell)   |    ✅    |   ✅ (H5AD/RDS)   |      none²       |
| OpenML (ML datasets)         |    ✅    |     ✅ (ARFF)     |       md5        |
| RCSB PDB (structures)        |    ✅    |  ✅ (.cif/.pdb)   |      none²       |
| UniProtKB (proteins)         |    ✅    |    ✅ (FASTA)     |      none²       |
| BioStudies (EBI)             |    ✅    | ✅ (study files)  |      none²       |
| GBIF (biodiversity)          |    ✅    | ✅ (Darwin Core)⁴ |      none²       |
| data.gov (DCAT-US)           |    ✅    |  ✅ (file URL)⁴   |      none³       |
| NASA CMR (Earth science)     |    ✅    |        —⁵         |        —         |
| GWAS Catalog                 |    ✅    |   → PMID bridge   |        —         |

¹ Dryad downloads are token / bot-challenge gated, so `fetch` returns an error;
`resolve` still lists the files.

² No upstream checksum, so `fetch` does not verify these bytes. It still returns
an error on an HTTP error or when the download exceeds `max_bytes`.

³ No upstream checksum. Files declared as PDF or XML (literature full text, and
data.gov distributions with that mediaType) get an HTML sniff: an HTML login or
paywall page served in their place returns an error. Other files are not checked.

⁴ Only records that carry a downloadable file (a GBIF Darwin Core Archive, a
data.gov distribution URL); metadata-only records are discovery-only.

⁵ Discovery-only: granule downloads need an Earthdata login, which is not wired.
`resolve` returns the DOI and a data-access portal link.

## Reference

Every tool and parameter, the HTTP transport (`--transport http`), and the
environment variables: **[docs/reference.md](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/reference.md)**.

## Develop

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest -q
uv run ruff check src tests
DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -k live -q   # real-API probes
```

The README demo (`examples/assets/demo.svg`) is recorded from live calls by
`examples/_demo_search.py`; its header has the commands to re-record it.

## License

MIT — see [LICENSE](https://github.com/musharna/data-aggregator-mcp/blob/main/LICENSE).
