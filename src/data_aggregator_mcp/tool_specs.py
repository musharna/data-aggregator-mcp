"""Static MCP wire specs — the tool JSON schemas and the prompt catalog.

Declarative data plus the prompt-text templates: no I/O and no router import. It lives
apart from ``server`` so that module stays request-handling logic rather than ~450 lines
of literal. ``server`` re-exports these under their historical names.

The few non-literal values are schema defaults sourced from the modules that enforce them
(``zenodo`` page sizes, the ``fetch`` byte ceiling) plus the pydantic output schemas, so an
advertised default cannot drift from the limit actually applied.

Every string here is client-visible: tool/prompt names, argument schemas and descriptions
are a public contract, so edits are behaviour changes even though nothing executes.
"""

from __future__ import annotations

from mcp import types

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import zenodo
from data_aggregator_mcp.models import DataResource, FetchResult, RelateResult, SearchResult

TOOLS: list[types.Tool] = [
    types.Tool(
        name="search",
        description=(
            "Search public research-data archives, omics registries, and the "
            "literature for datasets, software, publications, and sequencing data. "
            "Fans out across Zenodo, DataCite (Dryad, Figshare, Dataverse, OSF, "
            "Mendeley, OpenNeuro), NCBI omics (GEO, SRA, BioProject), literature (PubMed + "
            "OpenAIRE), HuggingFace Hub (datasets), DataONE (eco/environmental federation), "
            "OmicsDI (proteomics/metabolomics), RCSB PDB (macromolecular structures), "
            "GWAS Catalog (genotype-phenotype studies), OpenML (ML datasets), "
            "DANDI (neurophysiology dandisets), and CZ CELLxGENE (single-cell datasets). "
            "Returns compact DataResource "
            "records; per-source failures are "
            "reported in errors{}. Use resolve for the full record (SRA resolve attaches "
            "the ENA FASTQ manifest; publication resolve attaches links[] to datasets/"
            "accessions, normalized identifiers (pmid/pmcid/doi), and — when open access — "
            "a full-text file), then fetch to download files."
            " Pass organism=<name> to expand the query with NCBI-Taxonomy "
            "synonyms; results carry normalized taxa[] + plant cross-links."
            " Pass disease=<name> to expand the query with MeSH descriptor "
            "synonyms (e.g. 'breast cancer' also matches 'Breast Neoplasms'); "
            "the expansion is echoed in mesh_expansion."
            " Pass tissue=<name> to expand the query with UBERON synonyms "
            "(e.g. 'liver' also matches 'iecur'/'jecur'); the expansion is "
            "echoed in tissue_expansion."
            " Pass chemical=<name> to expand the query with ChEBI compound "
            "synonyms (e.g. 'caffeine' also matches '1,3,7-trimethylxanthine'); "
            "the expansion is echoed in chemical_expansion. Pass assay=<name> "
            "to expand the query with EDAM assay/method synonyms (e.g. 'ChIP-seq' "
            "also matches 'ChIP-sequencing'); echoed in assay_expansion."
            " Pass collapse_mirrors=true to opt into conservative cross-repo "
            "mirror collapse: same-dataset copies under different/no DOIs are "
            "folded into one record, with the folded copies annotated under "
            "mirrors[]."
            " An ontology param that matches no term in its registry (e.g. "
            "organism='yeast' — NCBI Taxonomy indexes no such common name) is "
            "reported in unresolved[] and the search runs WITHOUT that expansion, "
            "so a dropped filter is never silent. Clients that support form "
            "elicitation are asked for a replacement term before the search runs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search query"},
                "size": {
                    "type": "integer",
                    "description": "Max results (1-50, default 10)",
                    "default": zenodo.DEFAULT_SIZE,
                    "minimum": 1,
                    "maximum": zenodo.MAX_SIZE,
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict fan-out to these sources (default: all). "
                    "Available: zenodo, dataone, gbif, cellxgene, datacite, dandi, omics, "
                    "literature, huggingface, datagov, nasacmr, omicsdi, openml, pdb, "
                    "uniprot, gwas, biostudies",
                },
                "organism": {
                    "type": "string",
                    "description": "Optional organism name. Resolved via NCBI Taxonomy; "
                    "the query is expanded with the canonical name + synonyms (e.g. "
                    "'Orobanche aegyptiaca' also matches 'Phelipanche aegyptiaca'). The "
                    "expansion is echoed in taxon_expansion.",
                },
                "disease": {
                    "type": "string",
                    "description": "Optional disease/phenotype name. Resolved via MeSH "
                    "(NCBI E-utilities); the query is expanded with the canonical descriptor "
                    "+ entry-term synonyms (e.g. 'breast cancer' also matches "
                    "'Breast Neoplasms'). The expansion is echoed in mesh_expansion.",
                },
                "tissue": {
                    "type": "string",
                    "description": "Optional tissue/anatomy name. Resolved via UBERON (EBI OLS); "
                    "the query is expanded with the canonical term + exact synonyms (e.g. "
                    "'liver' also matches 'iecur'/'jecur'). "
                    "The expansion is echoed in tissue_expansion.",
                },
                "chemical": {
                    "type": "string",
                    "description": "Optional chemical/compound name. Resolved via ChEBI (EBI OLS); "
                    "the query is expanded with the canonical name + exact synonyms (e.g. "
                    "'caffeine' also matches '1,3,7-trimethylxanthine'), capped to a bounded "
                    "number of synonyms. An unknown term yields no expansion; an OLS failure "
                    "surfaces in errors. The expansion is echoed in chemical_expansion.",
                },
                "assay": {
                    "type": "string",
                    "description": "Optional assay/method name. Resolved via EDAM topics (EBI OLS); "
                    "the query is expanded with the canonical name + exact synonyms (e.g. "
                    "'ChIP-seq' also matches 'ChIP-sequencing'/'ChIP-exo'). An unknown term "
                    "yields no expansion; an OLS failure surfaces in errors. The expansion is "
                    "echoed in assay_expansion.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque pagination token from a prior search's next_cursor. "
                    "When set, all other search params are read from the cursor.",
                },
                "published_after": {
                    "type": "integer",
                    "description": "Keep results with year >= this.",
                },
                "published_before": {
                    "type": "integer",
                    "description": "Keep results with year <= this.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["dataset", "sequencing_run", "study", "publication", "software"],
                    "description": "Keep only results of this kind.",
                },
                "rank": {
                    "type": "string",
                    "enum": ["relevance", "semantic"],
                    "default": "relevance",
                    "description": (
                        "Result ordering. 'relevance' (default) = upstream/merged order. "
                        "'semantic' re-ranks the fetched page by embedding similarity to the "
                        "query (needs EMBEDDING_API_BASE; degrades to relevance order with an "
                        "errors['semantic'] note if unconfigured). In semantic mode pagination "
                        "is window-based (each page consumes its full fetched window)."
                    ),
                },
                "collapse_mirrors": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opt into conservative cross-repo content dedup (default false). "
                        "On top of the always-on exact-DOI dedup, folds records that are the "
                        "SAME dataset deposited under different (or no) DOIs — e.g. a Zenodo "
                        "mirror of a figshare deposit, GEO<->ArrayExpress — into one record, "
                        "annotating the survivor with the folded copies under mirrors[]. "
                        "Conservative: a merge needs a shared file checksum OR identical "
                        "(normalized-title, first-author-surname, year); title-only or partial "
                        "matches never merge. Intra-page / best-effort only (a mirror on a "
                        "different page is not collapsed), so a page may return fewer than size "
                        "items; pagination is unaffected."
                    ),
                },
                "understand": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opt into LLM query understanding: a free-text query is rewritten into "
                        "a keyword core + structured params (organism/disease/tissue/chemical/"
                        "assay, kind, year) before fan-out; extracted entities are validated by "
                        "the same ontology resolvers (a hallucinated entity that doesn't resolve "
                        "is simply dropped), explicit params you pass always win, and the "
                        "interpretation is echoed in query_understanding. Requires an LLM endpoint "
                        "(LLM_API_BASE); with none configured the search runs unchanged and notes "
                        "it in errors['understand']."
                    ),
                },
                "multi_query": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opt into diverse multi-query recall expansion: an LLM generates up to a "
                        "few deliberately-diverse reformulations of your query, each is fanned out "
                        "across all sources, and the deduped union is re-ranked against your "
                        "original query — surfacing relevant records a single keyword query would "
                        "miss. Costs N× the upstream calls (bounded). Requires an LLM endpoint "
                        "(LLM_API_BASE); with none configured the search runs as a normal single "
                        "query and notes it in errors['multi_query']. The variants used are echoed "
                        "in query_expansion. Composes with understand=. "
                        "NOTE: multi_query=true ALWAYS applies semantic re-ranking of the window "
                        "internally regardless of rank=; the rank= param has no effect in this mode."
                    ),
                },
                "provenance": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Opt into a whole-search RO-Crate 1.1 Run Crate (default false). "
                        "Attaches provenance_crate{} — a machine-readable manifest documenting "
                        "this search: the query, the sources queried, the ontology expansions "
                        "that fired, the per-source errors (a partial search is disclosed), and "
                        "per-hit provenance for every result (version-currency, licence + "
                        "normalized SPDX, FAIR score). Per-hit RETRACTION is omitted — it would "
                        "need one Crossref call per hit; use per-record resolve(format=provenance) "
                        "for that. Covers THIS search page only (intra-page; each page of a "
                        "paginated search gets its own crate)."
                    ),
                },
            },
        },
        outputSchema=SearchResult.model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="resolve",
        description=(
            "Fetch the full DataResource for a known id (e.g. 'zenodo:7654321', "
            "'datacite:10.5061/dryad.x', 'hf:owner/name', a bare Zenodo record id, or a DOI), "
            "including the complete files[] manifest. Publication resolve also attaches "
            "normalized identifiers (pmid/pmcid/doi) and, when open access, a full-text file. "
            "Pass cite=<format> to render a "
            "citation onto the result (citation field); omitted means no citation. "
            "Pass trust=true to attach retraction status (via Crossref) under trust{}. "
            "Pass fair=true to attach an RDA-grounded FAIRness score (0–100 + F/A/I/R "
            "sub-scores + actionable gaps) computed from the record under fair{}. "
            "Pass use=<intent> (commercial/redistribute/modify/ml-training) to attach a "
            "licence-compatibility advisory (ALLOW/REVIEW/DENY, not legal advice) under "
            "license_compat{}. "
            "Pass format=provenance for a one-call RO-Crate 1.1 data-availability dossier "
            "(under provenance{}) composing version-currency, licence+SPDX, FAIR score, "
            "retraction status, and the source/DOI/ID chain — it auto-attaches fair + trust."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Source-prefixed id, bare Zenodo id, or DOI",
                },
                "cite": {
                    "type": "string",
                    "description": "Optional citation format to render onto the result: "
                    "'bibtex', 'ris', 'csl-json', or any CSL style name ('apa', 'mla', "
                    "'vancouver', ...). DOI-bearing records render via DOI content "
                    "negotiation; non-DOI records support 'csl-json' only. Omitted = no "
                    "citation. Failures degrade quietly (citation stays null).",
                },
                "format": {
                    "type": "string",
                    "enum": ["croissant", "ro-crate", "provenance"],
                    "description": "Optional export to render onto the result. 'croissant' "
                    "attaches a file-level Croissant JSON-LD manifest (croissant field); "
                    "'ro-crate' attaches a minimal RO-Crate 1.1 manifest (ro_crate field); "
                    "'provenance' attaches a one-call RO-Crate 1.1 data-availability dossier "
                    "(provenance field) bundling version-currency, licence+SPDX, FAIR score, "
                    "retraction status, and the source/DOI/ID chain — it auto-attaches fair{} "
                    "and trust{} so the dossier is complete in one call (unknown signals are "
                    "reported as unknown, never as a clean claim).",
                },
                "trust": {
                    "type": "boolean",
                    "description": "When true, attach trust signals (retraction status via "
                    "Crossref) to the result under trust{}. One extra Crossref call; only "
                    "meaningful for DOI-bearing records (a DataCite data DOI Crossref does not "
                    "register leaves retracted=null = unknown, never a false clean claim).",
                },
                "fair": {
                    "type": "boolean",
                    "description": "When true, attach an RDA-grounded FAIRness assessment under "
                    "fair{}: a 0–100 overall score plus findable/accessible/interoperable/reusable "
                    "sub-scores, the count of indicators evaluated, and actionable gaps each naming "
                    "its RDA FAIR Data Maturity Model indicator id. Pure/local — no network call. "
                    "Only the machine-evaluable subset is scored (never fabricates what the "
                    "metadata cannot show).",
                },
                "use": {
                    "type": "string",
                    "description": "When set, attach a licence-compatibility advisory under "
                    "license_compat{} for an intended use of the record. Supported intents: "
                    "'commercial', 'redistribute', 'modify', 'ml-training' (training = a "
                    "derivative+commercial use, our stated interpretation). The verdict is "
                    "ALLOW/REVIEW/DENY computed from a bundled choosealicense.com licence "
                    "matrix keyed on the normalized SPDX id, naming the governing clause — a "
                    "metadata-derived advisory, NOT legal advice. An unrecognized or absent "
                    "licence yields REVIEW (never a fabricated ALLOW/DENY); an unknown intent "
                    "is an error.",
                },
            },
            "required": ["id"],
        },
        outputSchema=DataResource.model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="fetch",
        description=(
            "Download a resource's files to local disk and return the PATHS (never "
            "the file contents). Fetchable backends: "
            "Zenodo (md5-verified); "
            "SRA via ENA FASTQ (md5-verified); "
            "GEO supplementary files (unverified); "
            "DataCite sub-repos — Figshare/Dataverse/OSF (md5-verified), "
            "OpenNeuro (snapshot manifest, unverified), "
            "Dryad is manifest-only (resolve lists files, fetch fails loud), "
            "Mendeley + other DataCite repos fail loud; "
            "PubMed/OpenAIRE open-access full text (EuropePMC XML / Unpaywall PDF, unverified); "
            "HuggingFace Hub (unverified); "
            "DataONE Member-Node objects (md5/SHA-256-verified); "
            "OmicsDI — PRIDE + MetaboLights only (unverified), "
            "MassIVE/GNPS/PeptideAtlas/Metabolomics Workbench fail loud; "
            "DANDI dandisets (302→S3, unverified); "
            "CZ CELLxGENE H5AD/RDS assets (unverified); "
            "OpenML ARFF (md5-verified); "
            "RCSB PDB .cif/.pdb structure files (unverified). "
            "Fails loud if selected files exceed max_bytes unless force=true. "
            "Verifies checksums; writes a .dataresource.json sidecar."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Source-prefixed id or bare Zenodo id"},
                "dest": {
                    "type": "string",
                    "description": "Destination dir (default managed cache)",
                },
                "files": {"type": "string", "description": "Glob over file names (default all)"},
                "max_bytes": {
                    "type": "integer",
                    "description": "Byte ceiling before failing loud",
                    "default": fetch_mod.DEFAULT_MAX_BYTES,
                },
                "force": {"type": "boolean", "description": "Override max_bytes", "default": False},
                "extract": {
                    "type": "boolean",
                    "description": "Unpack downloaded zip/tar archives into the destination "
                    "(default false). Path-traversal-guarded; counts against max_bytes.",
                    "default": False,
                },
            },
            "required": ["id"],
        },
        outputSchema=FetchResult.model_json_schema(),
        annotations=types.ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False
        ),
    ),
    types.Tool(
        name="list_sources",
        description=(
            "List wired data sources and their capabilities (layer, kinds, supported "
            "filters, auth requirement, rate limit, status)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "check_health": {
                    "type": "boolean",
                    "description": (
                        "When true, probe 5 sources (zenodo, datacite, omics, literature, "
                        "huggingface) and attach a 'health' field "
                        "({status: up|down, latency_ms, detail}) to those entries; every "
                        "other source gets health: null. "
                        "Default false: returns the static catalog with no network."
                    ),
                    "default": False,
                },
            },
        },
        outputSchema={
            "type": "object",
            "properties": {"sources": {"type": "array", "items": {"type": "object"}}},
            "required": ["sources"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="operate",
        description=(
            "Inspect or query a remote tabular file (Parquet/CSV/TSV) WITHOUT downloading "
            "it. op='schema' returns columns+types; 'preview' a small sample; 'head' the "
            "first n rows; 'sql' a read-only SELECT against the file (exposed as the view "
            "'data', e.g. \"SELECT * FROM data WHERE x > 1\"). op='peek' profiles every "
            "column WITHOUT downloading — type, null-rate, approximate distinct count, "
            "min/max, and numeric quartiles (a DuckDB SUMMARIZE; like head/sql it reads the "
            "whole file, so it honors the source-size ceiling). Addresses a file by catalog "
            "id + file name (resolve the id first to see files[] and access_modes). Requires "
            "the [operate] extra; fails loud if the file is not an operable tabular file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["schema", "preview", "head", "sql", "peek"]},
                "id": {"type": "string", "description": "DataResource id (e.g. 'zenodo:7654321')"},
                "file": {
                    "type": "string",
                    "description": "File name within the record; optional when exactly one "
                    "operable file is present.",
                },
                "query": {"type": "string", "description": "Read-only SELECT for op='sql'."},
                "n": {
                    "type": "integer",
                    "description": "Row count for head/preview",
                    "default": 20,
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional column projection for head.",
                },
            },
            "required": ["op", "id"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="relate",
        description=(
            "Given 2-10 resource ids, return metadata-level join/harmonization HINTS: how "
            "the datasets relate and on what key they could be joined. Detects shared "
            "accessions (BioProject/SRA/GEO), shared cross-identifiers (doi/pmid/pmcid), "
            "explicit links between the inputs, and version lineage. HINTS ONLY — it does "
            "not read file columns, fetch files, or execute any join/merge/conversion; each "
            "hint names the shared value as evidence. Resolve ids first if you only have a "
            "search result. Per-id resolve failures are reported, not fatal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 10,
                    "description": "2-10 source-prefixed resource ids to relate.",
                },
            },
            "required": ["ids"],
        },
        outputSchema=RelateResult.model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
]


PROMPTS: list[types.Prompt] = [
    types.Prompt(
        name="find_data",
        description="Find datasets/data for a topic, optionally scoped to an organism.",
        arguments=[
            types.PromptArgument(
                name="topic", description="What to find data about", required=True
            ),
            types.PromptArgument(
                name="organism",
                description="Optional organism to expand via NCBI Taxonomy",
                required=False,
            ),
        ],
    ),
    types.Prompt(
        name="data_behind_paper",
        description="Find the datasets / accessions behind a paper (by DOI, PMID, or title).",
        arguments=[
            types.PromptArgument(
                name="paper", description="DOI, 'pubmed:<id>', or paper title", required=True
            ),
        ],
    ),
    types.Prompt(
        name="search_resolve_fetch",
        description="Walk the search → resolve → fetch flow for a data need.",
        arguments=[
            types.PromptArgument(name="need", description="What data is needed", required=True),
        ],
    ),
]


def prompt_text(name: str, args: dict[str, str]) -> str:
    if name == "find_data":
        topic = args.get("topic", "")
        organism = args.get("organism")
        org = (
            f" Pass organism='{organism}' to expand the query with NCBI-Taxonomy synonyms."
            if organism
            else ""
        )
        return (
            f"Use the data-aggregator `search` tool to find datasets about: {topic}.{org} "
            "Review the compact results, then `resolve` the most relevant id for its full "
            "files[] manifest, and `fetch` to download."
        )
    if name == "data_behind_paper":
        paper = args.get("paper", "")
        return (
            f"Find the data behind '{paper}'. If it is a DOI/PMID, `resolve` it — publication "
            "resolve attaches links[] to datasets/accessions and normalized identifiers. Then "
            "`resolve`/`fetch` each linked dataset. Otherwise `search` for the paper first."
        )
    if name == "search_resolve_fetch":
        need = args.get("need", "")
        return (
            f"Goal: {need}. 1) `search` (add organism= to expand taxonomy synonyms). "
            "2) `resolve` a chosen id for the full record + files[]. 3) `fetch` to download. "
            "Use `list_sources` to see which sources are fetchable."
        )
    raise ValueError(f"unknown prompt: {name}")
