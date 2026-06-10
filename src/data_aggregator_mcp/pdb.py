"""RCSB Protein Data Bank — macromolecular structures.

Two endpoints: the search API returns ranked entry IDs only, so a single GraphQL
batch call hydrates titles + primary-citation DOI/PubMed (the literature bridge)
for the whole page. Structure files (.cif/.pdb) stream from files.rcsb.org with no
upstream checksum -> fetch is unverified, not operable. kind="dataset".
"""

from __future__ import annotations

import json

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, FileEntry, Link, compact

SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
GRAPHQL = "https://data.rcsb.org/graphql"
_DOWNLOAD = "https://files.rcsb.org/download/{id}.{ext}"
_LANDING = "https://www.rcsb.org/structure/{id}"
PREFIXES = {"pdb"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

_GQL = (
    "{{entries(entry_ids:[{ids}]){{rcsb_id struct{{title}} "
    "rcsb_accession_info{{initial_release_date}} "
    "rcsb_primary_citation{{year pdbx_database_id_DOI pdbx_database_id_PubMed}} "
    "rcsb_entry_info{{experimental_method}}}}}}"
)


def _search_body(query: str, start: int, rows: int) -> str:
    return json.dumps(
        {
            "query": {
                "type": "terminal",
                "service": "full_text",
                "parameters": {"value": query},
            },
            "return_type": "entry",
            "request_options": {"paginate": {"start": start, "rows": rows}},
        }
    )


async def _hydrate(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    gql = _GQL.format(ids=",".join(f'"{i}"' for i in ids))
    body = await _http.request_json(
        client,
        "POST",
        GRAPHQL,
        service="RCSB PDB graphql",
        content=json.dumps({"query": gql}),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    entries = ((body or {}).get("data") or {}).get("entries") or []
    return {e["rcsb_id"]: e for e in entries if e and e.get("rcsb_id")}


def _normalize(entry: dict) -> DataResource:
    rid = entry["rcsb_id"]
    cite = entry.get("rcsb_primary_citation") or {}
    pubmed = cite.get("pdbx_database_id_PubMed")
    identifiers: dict[str, str] = {}
    if pubmed:
        identifiers["pmid"] = str(pubmed)
    method = (entry.get("rcsb_entry_info") or {}).get("experimental_method")
    return DataResource(
        id=f"pdb:{rid}",
        source="pdb",
        kind="dataset",
        title=(entry.get("struct") or {}).get("title") or "",
        doi=cite.get("pdbx_database_id_DOI"),
        year=cite.get("year"),
        identifiers=identifiers,
        subjects=[method] if method else [],
        last_updated=(entry.get("rcsb_accession_info") or {}).get("initial_release_date"),
        files=[],
        links=[Link(rel="landing_page", target_id=_LANDING.format(id=rid))],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    rows = min(size, MAX_SIZE)
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="RCSB PDB search",
        params={"json": _search_body(query, offset, rows)},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={"total_count": 0, "result_set": []},
    )
    total = (body or {}).get("total_count", 0)
    ids = [hit["identifier"] for hit in (body or {}).get("result_set") or []]
    meta = await _hydrate(client, ids)
    recs = [compact(_normalize(meta[i])) for i in ids if i in meta]
    return total, recs


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    pid = resource_id.split(":", 1)[1].strip().upper() if ":" in resource_id else ""
    if not pid:
        raise NotFoundError(f"malformed PDB id {resource_id!r}")
    meta = await _hydrate(client, [pid])
    entry = meta.get(pid)
    if entry is None:
        raise NotFoundError(f"RCSB PDB has no entry {pid}")
    resource = _normalize(entry)
    files = [
        FileEntry(
            name=f"{pid}.{ext}",
            url=_DOWNLOAD.format(id=pid, ext=ext),
            mime="chemical/x-cif" if ext == "cif" else "chemical/x-pdb",
            source="rcsb",
        )
        for ext in ("cif", "pdb")
    ]
    return resource.model_copy(update={"files": files})
