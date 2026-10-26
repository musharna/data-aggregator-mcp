# data-aggregator-mcp

data-aggregator-mcp is an MCP server that lets AI agents search 21 public
research-data sources at once and get back one deduplicated list.

[![CI](https://img.shields.io/github/actions/workflow/status/musharna/data-aggregator-mcp/ci.yml?branch=main&label=CI)](https://github.com/musharna/data-aggregator-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/data-aggregator-mcp.svg)](https://pypi.org/project/data-aggregator-mcp/)
[![License](https://img.shields.io/pypi/l/data-aggregator-mcp.svg)](https://github.com/musharna/data-aggregator-mcp/blob/main/LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21636332.svg)](https://doi.org/10.5281/zenodo.21636332)
[![Glama](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/data-aggregator-mcp)

<!--
mcp-name: io.github.musharna/data-aggregator-mcp
-->

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

One-click install for VS Code or Cursor:

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install-0098FF?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=data-aggregator&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22data-aggregator-mcp%22%5D%7D)
[![Install in Cursor](https://img.shields.io/badge/Cursor-Install-000000?logo=cursor&logoColor=white)](https://cursor.com/install-mcp?name=data-aggregator&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkYXRhLWFnZ3JlZ2F0b3ItbWNwIl19)

Any other MCP client: run `uvx data-aggregator-mcp` as a stdio server.

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

**"Find transcriptomes for Orobanche aegyptiaca."** The species was renamed; NCBI
Taxonomy maps it to *Phelipanche aegyptiaca*, and the sources that accept synonyms
match both names (the demo above).

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

The verdict is read from the record's license metadata; it is advice, not a legal
opinion. `resolve` also renders citations in any CSL style, checks Crossref for
retractions, scores FAIRness, and exports Croissant or RO-Crate. `relate` reports
how a set of records connect (a shared accession, DOI or explicit link).

## How a search runs

<p align="center">
  <img src="https://raw.githubusercontent.com/musharna/data-aggregator-mcp/main/docs/assets/architecture.svg"
       alt="How one search runs: NCBI Taxonomy maps Orobanche aegyptiaca to its current name, Phelipanche aegyptiaca, and the sources that accept synonyms search both names; the query goes to 21 sources in parallel (11 archives, 9 omics, 1 literature); records that share a DOI become one, keeping the copy that can be downloaded; a record can then be resolved to files and checksums, fetched with the checksum verified where the source publishes one, or queried with SQL without downloading it"
       width="820">
</p>

A source that fails is named in the result's `errors`, never dropped silently.

What each source can fetch, and which checksum it is verified against:
[docs/sources.md](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/sources.md).

## What the benchmark shows

An agent using this server was compared with the same agent using web search, on held-out
dataset-finding tasks scored by recall against independently built answer keys, 3 runs per
task. Mean recall, server against web search: Claude Haiku 5.5 0.47 against 0.32 (higher on
8 of 10 tasks), Sonnet 5.5 0.54 against 0.52 (5 of 10), Opus 5.5 0.72 against 0.58 (6 of 10).
With Haiku the server met round 5's pre-registered "better" rule (higher on at least 8 of 10
tasks, interval above 0) but not the stricter bar for a win across model tiers (9 of 10,
Holm-adjusted sign test, p = 0.22); Sonnet and Opus met neither. With Opus it returned fewer wrong
entries and answered in about a third of the time; with Sonnet it was faster on 9 of 10
tasks; with Haiku it cost less on 9 of 10.

The two latest Opus 5.5 rounds (2026-10-19, rules written before the runs):

| Tools given to the agent | Round 4 (5 tasks) | Round 5 (10 tasks) |
| --- | :---: | :---: |
| this server | 0.66 | 0.72 |
| web search | 0.66 | 0.58 |
| ToolUniverse | 0.59 | 0.63 |
| no tools (memory only) | 0.02 | 0.09 |

In round 5 this server was higher on 6 of 10 tasks, where the pre-set rule needed 8.
Web search hit the 15-minute limit in 11 of 30 runs, and a run that times out scores
0. In runs that finished, web search found more (0.90). The server answered in 203 s
against 599 s (median), with fewer tool calls and fewer wrong entries (precision 0.95
against 0.62). It cost more per run than web search ($2.08 against $1.51,
API-equivalent).

We chose the tasks and ran the benchmark ourselves; an outside re-run is welcome. Method
and controls:
[benchmarks/h2h](https://github.com/musharna/data-aggregator-mcp/blob/main/benchmarks/h2h/README.md);
the Haiku and Sonnet runs:
[2026-10-20 re-run](https://github.com/musharna/data-aggregator-mcp/blob/main/benchmarks/h2h/README.md#haiku-and-sonnet-re-run-2026-10-20).

## Compared with other servers

| Server | Searches | One search | Checksum | Remote SQL | License check |
| --- | --- | :---: | :---: | :---: | :---: |
| **data-aggregator-mcp** | 21 sources: data archives, omics, papers | yes, DOI dedup | where published | yes | yes |
| [Mobus](https://github.com/mobus-ai/Mobus) | 20 general and ML data platforms | yes | — | row preview | yes |
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | papers: arXiv, PubMed, OpenAlex, Crossref and more | yes, deduplicated | — | — | — |
| [ToolUniverse](https://github.com/mims-harvard/ToolUniverse) | 1000+ tools: models, datasets, APIs, packages | papers | — | — | — |
| [BioMCP](https://github.com/genomoncology/biomcp) | genes, variants, trials, drugs, proteins, papers | yes (`search all`) | — | — | — |

— means the project's README doesn't describe it (READMEs read 2026-10-04).
Where they are ahead: BioMCP reaches clinical trials, ChEMBL and AlphaFold;
ToolUniverse covers far more ground overall; paper-search-mcp covers far more
literature sources; Mobus also compares datasets and checks schema compatibility.
More: [docs/POSITIONING.md](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/POSITIONING.md).

## Reference and development

Every tool and parameter, the HTTP transport (`--transport http`) and the
environment variables:
[docs/reference.md](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/reference.md).
Sources that need every word of a query, and how they are searched:
[every-word sources](https://github.com/musharna/data-aggregator-mcp/blob/main/docs/reference.md#every-word-sources).
Tests, linting and the live API probes:
[CONTRIBUTING.md](https://github.com/musharna/data-aggregator-mcp/blob/main/CONTRIBUTING.md).

## License

MIT. See [LICENSE](https://github.com/musharna/data-aggregator-mcp/blob/main/LICENSE).
