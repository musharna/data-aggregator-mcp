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
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from data_aggregator_mcp import citation
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import router, zenodo
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource, FetchResult, SearchResult

_FETCHABLE_SOURCES = (
    "zenodo:",
    "sra:",
    "geo:",
    "datacite:",
    "pubmed:",
    "openaire:",
)  # id prefixes with a working fetch backend


def _is_fetchable(fid: str) -> bool:
    """True if ``fid`` has a wired fetch backend (allowlisted prefix or bare Zenodo id)."""
    return fid.startswith(_FETCHABLE_SOURCES) or fid.isdigit()


# DataCite ids all share the `datacite:` prefix, so fetchability is decided
# post-resolve from the detected host repo. Dryad is manifest-only (downloads are
# token/bot-challenge gated), so it is NOT here.
_DATACITE_FETCHABLE = ("figshare", "dataverse", "osf", "zenodo")


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
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "~60/min anonymous",
        "status": "live",
        "fetchable": True,
        "id_example": "zenodo:7654321",
    },
    {
        "name": "datacite",
        "layer": "archives",
        "kinds": ["dataset", "publication", "software"],
        "filters_supported": ["query"],
        "auth_required": False,
        "rate_limit": "respects 429/Retry-After",
        "status": "live (discovery; fetch on resolve for Figshare/Dataverse/OSF/Zenodo, manifest-only for Dryad)",
        "fetchable": "per-repo",
        "fetchable_notes": "Figshare/Dataverse/OSF/Zenodo fetchable; Dryad manifest-only (token/bot-gated); Mendeley + other repos discovery-only.",
        "id_example": "datacite:10.5061/dryad.x",
    },
    {
        "name": "omics",
        "layer": "omics",
        "kinds": ["study", "sequencing_run"],
        "filters_supported": ["query", "organism"],
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
        "filters_supported": ["query", "organism"],
        "auth_required": False,
        "rate_limit": "NCBI 3/s (10/s with NCBI_API_KEY); OpenAIRE + ScholeXplorer unmetered",
        "status": "live (discovery + resolve-time data links + identifiers; fetch retrieves open-access full text via EuropePMC/Unpaywall)",
        "fetchable": "open-access only",
        "fetchable_notes": "Open-access full text fetchable (EuropePMC XML / Unpaywall PDF, unverified); paywalled/non-OA ids fail loud.",
        "id_example": "pubmed:23066504 | openaire:<id>",
    },
]

TOOLS: list[types.Tool] = [
    types.Tool(
        name="search",
        description=(
            "Search public research-data archives, omics registries, and the "
            "literature for datasets, software, publications, and sequencing data. "
            "Fans out across Zenodo, DataCite (Dryad, Figshare, Dataverse, OSF, "
            "Mendeley), NCBI omics (GEO, SRA, BioProject), and literature (PubMed + "
            "OpenAIRE). Returns compact DataResource records; per-source failures are "
            "reported in errors{}. Use resolve for the full record (SRA resolve attaches "
            "the ENA FASTQ manifest; publication resolve attaches links[] to datasets/"
            "accessions, normalized identifiers (pmid/pmcid/doi), and — when open access — "
            "a full-text file), then fetch to download files."
            " Pass organism=<name> to expand the query with NCBI-Taxonomy "
            "synonyms; results carry normalized taxa[] + plant cross-links."
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
                    "Available: zenodo, datacite, omics, literature",
                },
                "organism": {
                    "type": "string",
                    "description": "Optional organism name. Resolved via NCBI Taxonomy; "
                    "the query is expanded with the canonical name + synonyms (e.g. "
                    "'Orobanche aegyptiaca' also matches 'Phelipanche aegyptiaca'). The "
                    "expansion is echoed in taxon_expansion.",
                },
            },
            "required": ["query"],
        },
        outputSchema=SearchResult.model_json_schema(),
    ),
    types.Tool(
        name="resolve",
        description=(
            "Fetch the full DataResource for a known id (e.g. 'zenodo:7654321', "
            "'datacite:10.5061/dryad.x', a bare Zenodo record id, or a DOI), "
            "including the complete files[] manifest. Publication resolve also attaches "
            "normalized identifiers (pmid/pmcid/doi) and, when open access, a full-text file. "
            "Pass cite=<format> to render a "
            "citation onto the result (citation field); omitted means no citation."
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
            },
            "required": ["id"],
        },
        outputSchema=DataResource.model_json_schema(),
    ),
    types.Tool(
        name="fetch",
        description=(
            "Download a resource's files to local disk and return the PATHS (never "
            "the file contents). Fetchable: Zenodo, SRA (ENA FASTQ), GEO supplementary files, and "
            "DataCite-discovered Figshare/Dataverse/OSF deposits (md5-verified), "
            "and open-access literature full text (EuropePMC XML / Unpaywall PDF, unverified); "
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
    ),
    types.Tool(
        name="list_sources",
        description=(
            "List wired data sources and their capabilities (layer, kinds, supported "
            "filters, auth requirement, rate limit, status)."
        ),
        inputSchema={"type": "object", "properties": {}},
        outputSchema={
            "type": "object",
            "properties": {"sources": {"type": "array", "items": {"type": "object"}}},
            "required": ["sources"],
        },
    ),
]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "list_sources":
        return {"sources": _SOURCES}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        match name:
            case "search":
                total, results, errors, expansion = await router.search(
                    client,
                    args["query"],
                    size=args.get("size", zenodo.DEFAULT_SIZE),
                    sources=args.get("sources"),
                    organism=args.get("organism"),
                )
                return SearchResult(
                    query=args["query"],
                    total=total,
                    count=len(results),
                    results=results,
                    errors=errors,
                    taxon_expansion=expansion,
                ).model_dump()
            case "resolve":
                resource = await router.resolve(client, args["id"])
                cite = args.get("cite")
                if cite:
                    rendered = await citation.render(client, resource, cite)
                    resource = resource.model_copy(update={"citation": rendered})
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
                out = await fetch_mod.fetch_files(
                    client,
                    resource,
                    dest=args.get("dest"),
                    files=args.get("files"),
                    max_bytes=args.get("max_bytes", fetch_mod.DEFAULT_MAX_BYTES),
                    force=args.get("force", False),
                    extract=args.get("extract", False),
                )
                return out.model_dump()
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
