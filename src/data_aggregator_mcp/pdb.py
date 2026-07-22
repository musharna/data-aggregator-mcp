"""RCSB Protein Data Bank — macromolecular structures.

Two endpoints: the search API returns ranked entry IDs only, so a single GraphQL
batch call hydrates titles + primary-citation DOI/PubMed (the literature bridge)
for the whole page. Structure files (.cif/.pdb) stream from files.rcsb.org with no
upstream checksum -> fetch is unverified, not operable. kind="dataset".
"""

from __future__ import annotations

import json
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    FundingRef,
    Link,
    Taxon,
    compact,
)

SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
GRAPHQL = "https://data.rcsb.org/graphql"
_DOWNLOAD = "https://files.rcsb.org/download/{id}.{ext}"
_LANDING = "https://www.rcsb.org/structure/{id}"
PREFIXES = {"pdb"}
# PDB entry ids are 4-char alphanumeric (classic) or extended `pdb_########`; this
# charset also guards resolve's user-supplied id from breaking the GraphQL string.
_PDB_ID_RE = re.compile(r"^[A-Za-z0-9_]{4,12}$")
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

_GQL = (
    "{{entries(entry_ids:[{ids}]){{rcsb_id struct{{title}} "
    "rcsb_accession_info{{initial_release_date}} "
    "rcsb_primary_citation{{year pdbx_database_id_DOI pdbx_database_id_PubMed}} "
    "rcsb_entry_info{{experimental_method}} "
    "audit_author{{name pdbx_ordinal}} "
    "pdbx_audit_support{{funding_organization grant_number}} "
    "polymer_entities{{rcsb_entity_source_organism{{ncbi_taxonomy_id ncbi_scientific_name}}}}}}}}"
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


def _creators(entry: dict) -> list[Creator]:
    """audit_author rows ordered by pdbx_ordinal → Creator(name). PDB carries no
    ORCID on the author record, so orcid stays None."""
    rows = [a for a in (entry.get("audit_author") or []) if a and a.get("name")]
    rows.sort(key=lambda a: a.get("pdbx_ordinal") or 0)
    return [Creator(name=a["name"]) for a in rows]


def _funding(entry: dict) -> list[FundingRef]:
    """pdbx_audit_support → FundingRef, keyed on a present funding_organization
    (nullable list; a sparse classic entry yields nothing — never a blank funder)."""
    out: list[FundingRef] = []
    for s in entry.get("pdbx_audit_support") or []:
        org = (s or {}).get("funding_organization")
        if org:
            out.append(FundingRef(funder=org, award=(s or {}).get("grant_number")))
    return out


def _taxa(entry: dict) -> list[Taxon]:
    """Source organisms across all polymer entities, deduped by taxid (first name
    wins). Requires both an int taxid and a scientific name."""
    seen: dict[int, Taxon] = {}
    for pe in entry.get("polymer_entities") or []:
        for org in (pe or {}).get("rcsb_entity_source_organism") or []:
            taxid = (org or {}).get("ncbi_taxonomy_id")
            name = (org or {}).get("ncbi_scientific_name")
            if isinstance(taxid, int) and name and taxid not in seen:
                seen[taxid] = Taxon(taxid=taxid, name=name)
    return list(seen.values())


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
        creators=_creators(entry),
        funding=_funding(entry),
        doi=cite.get("pdbx_database_id_DOI"),
        year=cite.get("year"),
        identifiers=identifiers,
        taxa=_taxa(entry),
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
    if not _PDB_ID_RE.match(pid):
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
