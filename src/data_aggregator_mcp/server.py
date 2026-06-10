"""MCP server — exposes search/resolve/fetch/list_sources over stdio.

search/resolve fan out through the multi-source router (Zenodo + DataCite +
NCBI omics + literature: PubMed/OpenAIRE). fetch streams files for Zenodo,
SRA (ENA FASTQ, md5-verified), GEO supplementary records, and DataCite-discovered
Figshare/Dataverse/OSF deposits (md5-verified), and open-access literature full text
(EuropePMC XML / Unpaywall PDF, unverified); a DataCite Dryad id is manifest-only
(resolve lists files, fetch fails loud), and other DataCite repos plus paywalled/non-OA
literature ids fail loud.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from data_aggregator_mcp import citation, operate, router, zenodo
from data_aggregator_mcp import croissant as croissant_mod
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import health as health_mod
from data_aggregator_mcp import resources as resources_mod
from data_aggregator_mcp import ro_crate as ro_crate_mod
from data_aggregator_mcp import trust as trust_mod
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource, FetchResult, SearchResult

logger = logging.getLogger(__name__)

_FETCHABLE_SOURCES = (
    "zenodo:",
    "sra:",
    "geo:",
    "datacite:",
    "pubmed:",
    "openaire:",
    "hf:",
    "dataone:",
    "omicsdi:",
    "dandi:",
    "cellxgene:",
    "openml:",
    "pdb:",
)  # id prefixes with a working fetch backend


def _is_fetchable(fid: str) -> bool:
    """True if ``fid`` has a wired fetch backend (allowlisted prefix or bare Zenodo id)."""
    return fid.startswith(_FETCHABLE_SOURCES) or fid.isdigit()


# DataCite ids all share the `datacite:` prefix, so fetchability is decided
# post-resolve from the detected host repo. Dryad is manifest-only (downloads are
# token/bot-challenge gated), so it is NOT here.
_DATACITE_FETCHABLE = ("figshare", "dataverse", "osf", "zenodo", "openneuro")


def _ensure_repo_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when a datacite: id resolves to a host repo we can't stream."""
    if fid.startswith("datacite:") and resource.source not in _DATACITE_FETCHABLE:
        hint = (
            " Dryad downloads are token/bot-challenge gated." if resource.source == "dryad" else ""
        )
        raise FetchNotSupportedError(
            f"'{fid}' (repo: {resource.source}) is discovery-only for fetch — its file "
            f"manifest is available via resolve, but no adapter streams its bytes.{hint}"
        )


def _ensure_omicsdi_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when an omicsdi: id resolved to no files — its repo (MassIVE,
    Metabolomics Workbench, GNPS, PeptideAtlas) is discovery-only this wave.
    PRIDE/MetaboLights populate files[] at resolve and pass."""
    if fid.startswith("omicsdi:") and not resource.files:
        landing = next((lnk.target_id for lnk in resource.links if lnk.rel == "landing_page"), None)
        where = f" Fetch from the source repo directly: {landing}" if landing else ""
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — only PRIDE and MetaboLights records "
            f"are streamable; this repo exposes no wired fetch backend.{where}"
        )


_LITERATURE_PREFIXES = ("pubmed:", "openaire:")


def _ensure_fulltext_available(fid: str, resource: DataResource) -> None:
    """Fail loud when a literature id has no open-access full text to fetch
    (paywalled, or not in EuropePMC/Unpaywall) — don't return a silently empty
    result (spec §8)."""
    if fid.startswith(_LITERATURE_PREFIXES) and not resource.files:
        raise FetchNotSupportedError(
            f"'{fid}' has no open-access full text to fetch (it may be paywalled, "
            "or not in EuropePMC/Unpaywall). Resolve it for the landing page / DOI instead."
        )


server: Server = Server("data-aggregator-mcp")

_SOURCES: list[dict[str, Any]] = [
    {
        "name": "zenodo",
        "layer": "archives",
        "kinds": ["dataset", "publication", "software"],
        "filters_supported": [
            "query",
            "size",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ],
        "auth_required": False,
        "rate_limit": "~60/min anonymous",
        "status": "live",
        "fetchable": True,
        "operable": True,
        "id_example": "zenodo:7654321",
    },
    {
        "name": "datacite",
        "layer": "archives",
        "kinds": ["dataset", "publication", "software"],
        "filters_supported": [
            "query",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ],
        "auth_required": False,
        "rate_limit": "respects 429/Retry-After",
        "status": "live (discovery; fetch on resolve for Figshare/Dataverse/OSF/Zenodo, manifest-only for Dryad)",
        "fetchable": "per-repo",
        "operable": True,
        "fetchable_notes": "Figshare/Dataverse/OSF/Zenodo fetchable; OpenNeuro (10.18112/openneuro.*) datasets fetchable via the snapshot manifest; Dryad manifest-only (token/bot-gated); Mendeley + other repos discovery-only.",
        "id_example": "datacite:10.5061/dryad.x",
    },
    {
        "name": "omics",
        "layer": "omics",
        "kinds": ["study", "sequencing_run"],
        "filters_supported": [
            "query",
            "organism",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ],
        "auth_required": False,
        "rate_limit": "NCBI 3/s (10/s with NCBI_API_KEY); ENA unmetered",
        "status": "live (discovery; SRA FASTQ + GEO supplementary fetch on resolve)",
        "fetchable": "per-sub-source",
        "fetchable_notes": "SRA (ENA FASTQ, md5) + GEO supplementary fetchable; BioProject discovery-only (resolve attaches SRA-run links).",
        "id_example": "sra:SRX079566 | geo:GSE10072 | bioproject:PRJNA231221",
    },
    {
        "name": "literature",
        "layer": "literature",
        "kinds": ["publication"],
        "filters_supported": [
            "query",
            "organism",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ],
        "auth_required": False,
        "rate_limit": "NCBI 3/s (10/s with NCBI_API_KEY); OpenAIRE + ScholeXplorer unmetered",
        "status": "live (discovery + resolve-time data links + identifiers; fetch retrieves open-access full text via EuropePMC/Unpaywall)",
        "fetchable": "open-access only",
        "fetchable_notes": "Open-access full text fetchable (EuropePMC XML / Unpaywall PDF, unverified); paywalled/non-OA ids fail loud.",
        "id_example": "pubmed:23066504 | openaire:<id>",
    },
    {
        "name": "huggingface",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": [
            "query",
            "size",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ],
        "auth_required": False,
        "rate_limit": "HuggingFace Hub anonymous (generous)",
        "status": "live (discovery + resolve + fetch; contributes to page 1 only — HF paginates by cursor, not offset)",
        "fetchable": True,
        "operable": True,
        "fetchable_notes": "Files downloadable via the HF resolve URL (unverified — no checksum/size in the API).",
        "id_example": "hf:davidcechak/Arabidopsis_thaliana_DNA_v0",
        "description": "HuggingFace Hub datasets — searchable, resolvable, and fetchable via the resolve URL.",
    },
    {
        "name": "dataone",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size", "cursor"],
        "auth_required": False,
        "rate_limit": "public CN; courtesy only",
        "status": "live (eco/environmental federation; verified fetch via Member Nodes)",
        "fetchable": True,
        "operable": True,
        "fetchable_notes": "Data objects fetched from Member Nodes with per-object MD5/SHA-256 verification.",
        "id_example": "dataone:doi:10.18739/A26336",
        "description": "DataONE federation of environmental & earth-science repositories (KNB, Arctic Data Center, PANGAEA, TERN, ...).",
    },
    {
        "name": "omicsdi",
        "layer": "omics",
        "kinds": ["study"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (proteomics/metabolomics discovery; first page only)",
        "fetchable": "per-repo",
        "fetchable_notes": "PRIDE + MetaboLights records are fetchable (unverified - no upstream checksum); MassIVE/Metabolomics Workbench/GNPS/PeptideAtlas are discovery-only.",
        "id_example": "omicsdi:pride:PXD000001",
        "description": "Omics Discovery Index - proteomics & metabolomics studies; restricted to the mass-spec modality repos not already covered by the omics leg.",
    },
    {
        "name": "dandi",
        "layer": "omics",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (DANDI Archive search/resolve; asset-manifest fetch on resolve)",
        "fetchable": True,
        "operable": False,
        "fetchable_notes": "Assets stream from the DANDI API (302→S3, unverified — no checksum in the listing); the manifest is capped at the first 100 assets for large dandisets.",
        "id_example": "dandi:000004",
        "description": "DANDI Archive — neurophysiology dandisets (NWB); search + resolve with a per-asset download manifest.",
    },
    {
        "name": "openml",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (name-substring discovery, first page only; ARFF + Parquet fetch on resolve)",
        "fetchable": True,
        "operable": True,
        "fetchable_notes": "ARFF fetch is md5-verified; the auto-converted Parquet is operable (schema/preview/head/sql).",
        "id_example": "openml:61",
        "description": "OpenML machine-learning datasets — name-substring search; resolve attaches an md5-verified ARFF and an operable Parquet.",
    },
    {
        "name": "pdb",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (full-text discovery; .cif/.pdb structure fetch on resolve)",
        "fetchable": True,
        "operable": False,
        "fetchable_notes": "Structure files (.cif/.pdb) stream from files.rcsb.org (unverified — no upstream checksum).",
        "id_example": "pdb:1BG2",
        "description": "RCSB Protein Data Bank — macromolecular structures; full-text search, DOI/PMID-rich, .cif/.pdb fetch.",
    },
    {
        "name": "gwas",
        "layer": "omics",
        "kinds": ["study"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (disease-trait discovery; PubMed cross-link). Fetch not supported.",
        "fetchable": False,
        "fetchable_notes": "Discovery-only: study metadata + PMID bridge. Summary-statistics fetch is a future wave.",
        "id_example": "gwas:GCST000028",
        "description": "GWAS Catalog (EBI) — genome-wide association studies keyed by disease trait; DOI/PMID-rich, reinforces the paper-data bridge.",
    },
    {
        "name": "cellxgene",
        "layer": "omics",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (CZ CELLxGENE Discover collections search/resolve; asset manifest on resolve)",
        "fetchable": True,
        "operable": False,
        "fetchable_notes": "H5AD/RDS assets stream from datasets.cellxgene.cziscience.com (direct URLs, unverified — no checksum in the API); the per-collection manifest is capped at 200 files for large atlases.",
        "id_example": "cellxgene:col-lung-1",
        "description": "CZ CELLxGENE Discover — single-cell datasets grouped by collection (one publication DOI per collection); search filters on tissue/disease/organism/assay, resolve attaches the H5AD/RDS download manifest.",
    },
]

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
                    "Available: zenodo, datacite, omics, literature, huggingface, dataone, omicsdi",
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
            "Pass trust=true to attach retraction status (via Crossref) under trust{}."
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
                    "enum": ["croissant", "ro-crate"],
                    "description": "Optional export to render onto the result. 'croissant' "
                    "attaches a file-level Croissant JSON-LD manifest (croissant field); "
                    "'ro-crate' attaches a minimal RO-Crate 1.1 manifest (ro_crate field).",
                },
                "trust": {
                    "type": "boolean",
                    "description": "When true, attach trust signals (retraction status via "
                    "Crossref) to the result under trust{}. One extra Crossref call; only "
                    "meaningful for DOI-bearing records (a DataCite data DOI Crossref does not "
                    "register leaves retracted=null = unknown, never a false clean claim).",
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
            "the file contents). Fetchable: Zenodo, SRA (ENA FASTQ), GEO supplementary files, "
            "DataCite-discovered Figshare/Dataverse/OSF deposits (md5-verified), "
            "open-access literature full text (EuropePMC XML / Unpaywall PDF, unverified), "
            "and HuggingFace datasets (via the HF resolve URL, unverified); "
            "a DataCite Dryad id is manifest-only (resolve lists its files but fetch fails loud), "
            "and other DataCite repos plus paywalled/non-OA literature ids fail loud. "
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
                        "When true, probe each source's base endpoint and attach a "
                        "'health' field ({status: up|down, latency_ms, detail}) to each "
                        "source. Default false: returns the static catalog with no network."
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
            "'data', e.g. \"SELECT * FROM data WHERE x > 1\"). Addresses a file by catalog "
            "id + file name (resolve the id first to see files[] and access_modes). Requires "
            "the [operate] extra; fails loud if the file is not an operable tabular file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["schema", "preview", "head", "sql"]},
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
]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


_PROMPTS: list[types.Prompt] = [
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


@server.list_prompts()
async def _list_prompts() -> list[types.Prompt]:
    return _PROMPTS


def _prompt_text(name: str, args: dict[str, str]) -> str:
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


@server.get_prompt()
async def _get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = arguments or {}
    text = _prompt_text(name, args)
    return types.GetPromptResult(
        description=next((p.description for p in _PROMPTS if p.name == name), None),
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=text)),
        ],
    )


@server.list_resources()
async def _list_resources() -> list[types.Resource]:
    return resources_mod.static_resources()


@server.list_resource_templates()
async def _list_resource_templates() -> list[types.ResourceTemplate]:
    return resources_mod.templates()


@server.read_resource()
async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    if resources_mod.is_catalog(uri):
        payload = json.dumps({"sources": _SOURCES})
        return [ReadResourceContents(content=payload, mime_type="application/json")]
    rid = resources_mod.parse_record_id(uri)
    if rid is None:
        raise ValueError(f"not a readable data-aggregator resource: {uri}")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resource = await router.resolve(client, rid)
    return [ReadResourceContents(content=resource.model_dump_json(), mime_type="application/json")]


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "list_sources":
        if not args.get("check_health"):
            return {"sources": _SOURCES}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            probed = {h["name"]: h for h in await health_mod.probe_sources(client)}
        return {"sources": [{**s, "health": probed.get(s["name"])} for s in _SOURCES]}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        match name:
            case "search":
                result = await router.search_page(
                    client,
                    query=args.get("query"),
                    size=args.get("size", zenodo.DEFAULT_SIZE),
                    sources=args.get("sources"),
                    organism=args.get("organism"),
                    disease=args.get("disease"),
                    published_after=args.get("published_after"),
                    published_before=args.get("published_before"),
                    kind=args.get("kind"),
                    cursor=args.get("cursor"),
                    rank=args.get("rank", "relevance"),
                )
                return result.model_dump()
            case "resolve":
                resource = await router.resolve(client, args["id"])
                cite = args.get("cite")
                if cite:
                    rendered = await citation.render(client, resource, cite)
                    resource = resource.model_copy(update={"citation": rendered})
                fmt = args.get("format")
                if fmt == "croissant":
                    resource = resource.model_copy(
                        update={"croissant": croissant_mod.render(resource)}
                    )
                elif fmt == "ro-crate":
                    resource = resource.model_copy(
                        update={"ro_crate": ro_crate_mod.render(resource)}
                    )
                if args.get("trust"):
                    signals = await trust_mod.annotate(client, resource)
                    resource = resource.model_copy(update={"trust": signals})
                return resource.model_dump()
            case "fetch":
                fid = args["id"].strip()
                if not _is_fetchable(fid):
                    raise FetchNotSupportedError(
                        f"'{fid}' has no wired fetch backend. Fetchable id prefixes: "
                        f"{', '.join(_FETCHABLE_SOURCES)} (and bare Zenodo ids). "
                        "Resolve it for the landing page / DOI instead."
                    )
                resource = await router.resolve(client, fid)
                _ensure_repo_fetchable(fid, resource)
                _ensure_fulltext_available(fid, resource)
                _ensure_omicsdi_fetchable(fid, resource)
                # Wire MCP progress notifications when the caller supplied a
                # progressToken (in the request meta). The notification is
                # auxiliary telemetry: a send failure is logged and swallowed so
                # it can NEVER abort or mask the actual download. This is the one
                # sanctioned fail-soft spot — the core fetch still succeeds.
                try:
                    ctx = server.request_context
                except LookupError:
                    ctx = None  # called outside an MCP request (e.g. a unit test)
                token = getattr(getattr(ctx, "meta", None), "progressToken", None)
                on_progress = None
                if token is not None and ctx is not None:
                    session = ctx.session

                    async def _on_progress(done: int, total: int, name: str) -> None:
                        try:
                            await session.send_progress_notification(
                                token, progress=done, total=total
                            )
                        except Exception as exc:  # noqa: BLE001 - auxiliary telemetry
                            logger.warning("progress notification failed: %r", exc)

                    on_progress = _on_progress
                out = await fetch_mod.fetch_files(
                    client,
                    resource,
                    dest=args.get("dest"),
                    files=args.get("files"),
                    max_bytes=args.get("max_bytes", fetch_mod.DEFAULT_MAX_BYTES),
                    force=args.get("force", False),
                    extract=args.get("extract", False),
                    on_progress=on_progress,
                )
                return out.model_dump()
            case "operate":
                return await operate.run(
                    client,
                    args["id"],
                    args["op"],
                    file=args.get("file"),
                    query=args.get("query"),
                    n=args.get("n", 20),
                    columns=args.get("columns"),
                )
            case _:
                raise ValueError(f"unknown tool: {name}")


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch(name, arguments)


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
