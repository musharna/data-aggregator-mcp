"""RCSB Protein Data Bank — macromolecular structures.

Two endpoints: the search API returns ranked entry IDs only, so a single GraphQL
batch call hydrates titles, the entry's own DOI (10.2210/pdbXXXX/pdb) and the
primary-citation DOI/PubMed (the literature bridge: a ``described_in`` link + pmid)
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
    local_id,
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
MAX_RETRIES = 2
_ACCEPT_JSON = {"Accept": "application/json"}
# RCSB answers a zero-hit search with 204 No Content.
_NO_HITS = {"total_count": 0, "result_set": []}

_GQL = (
    "{{entries(entry_ids:[{ids}]){{rcsb_id struct{{title}} "
    "rcsb_accession_info{{initial_release_date}} "
    "rcsb_primary_citation{{year pdbx_database_id_DOI pdbx_database_id_PubMed}} "
    "rcsb_entry_info{{experimental_method}} "
    "pdbx_database_status{{pdb_format_compatible}} "
    "database_2{{database_id pdbx_DOI}} "
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


def _check_entries(data: dict) -> None:
    """``entries`` is null or a list of entry objects, each one null when RCSB has no
    such entry."""
    entries = data.get("entries")
    if not (
        "entries" in data
        and (
            entries is None
            or (
                isinstance(entries, list) and all(e is None or isinstance(e, dict) for e in entries)
            )
        )
    ):
        raise _http.UpstreamEnvelopeError(f"no entries list in {data!r:.200}")


async def _hydrate(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    gql = _GQL.format(ids=",".join(f'"{i}"' for i in ids))
    # A query RCSB rejects (a field renamed upstream) comes back as GraphQL errors and
    # raises; read as zero entries it made every resolve "no entry" and every search a
    # hit count with no records.
    data = await _http.graphql(
        client,
        GRAPHQL,
        gql,
        service="RCSB PDB graphql",
        max_retries=MAX_RETRIES,
        check=_check_entries,
    )
    return {e["rcsb_id"]: e for e in data["entries"] or [] if e and e.get("rcsb_id")}


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


def _entry_doi(entry: dict) -> str | None:
    """The structure's OWN DOI as registered by wwPDB (database_2 row with
    database_id == "PDB"), e.g. 10.2210/pdb6vxx/pdb. None when absent — never
    constructed from the id. Not the primary-citation DOI: many entries share one
    paper, and a shared ``doi`` makes DOI dedup fold distinct structures together."""
    for row in entry.get("database_2") or []:
        if (row or {}).get("database_id") == "PDB" and row.get("pdbx_DOI"):
            return str(row["pdbx_DOI"])
    return None


def _normalize(entry: dict) -> DataResource:
    rid = entry["rcsb_id"]
    cite = entry.get("rcsb_primary_citation") or {}
    pubmed = cite.get("pdbx_database_id_PubMed")
    identifiers: dict[str, str] = {}
    if pubmed:
        identifiers["pmid"] = str(pubmed)
    links = [Link(rel="landing_page", target_id=_LANDING.format(id=rid))]
    paper_doi = cite.get("pdbx_database_id_DOI")
    if paper_doi:
        links.append(Link(rel="described_in", target_id=paper_doi))
    method = (entry.get("rcsb_entry_info") or {}).get("experimental_method")
    return DataResource(
        id=f"pdb:{rid}",
        source="pdb",
        kind="dataset",
        title=(entry.get("struct") or {}).get("title") or "",
        creators=_creators(entry),
        funding=_funding(entry),
        doi=_entry_doi(entry),
        year=cite.get("year"),
        identifiers=identifiers,
        taxa=_taxa(entry),
        subjects=[method] if method else [],
        last_updated=(entry.get("rcsb_accession_info") or {}).get("initial_release_date"),
        links=links,
    )


def _check_search(body: dict) -> None:
    """A search 200 carries the hit count and the hit list. One without them is a
    malformed answer (retried, then an outage), never zero hits: those come as 204."""
    # type() rather than isinstance(): bool is an int subclass, and `true` is no count.
    if type(body.get("total_count")) is not int or not isinstance(body.get("result_set"), list):
        raise _http.UpstreamEnvelopeError(
            f"RCSB PDB search 200 without total_count and result_set: keys {sorted(body)}"
        )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    rows = min(size, MAX_SIZE)
    # httpx upper-cases the method, so "get" would send the same request.
    method = "GET"  # pragma: no mutate
    body = await _http.request_json(
        client,
        method,
        SEARCH,
        service="RCSB PDB search",
        params={"json": _search_body(query, offset, rows)},
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        # A 404 is NOT "no hits": it means the search endpoint itself is gone, so it
        # raises like any outage.
        no_content_returns=_NO_HITS,
        check=_check_search,
        expect=dict,
    )
    ids = [hit["identifier"] for hit in body["result_set"]]
    meta = await _hydrate(client, ids)
    recs = [compact(_normalize(meta[i])) for i in ids if i in meta]
    return body["total_count"], recs


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    pid = local_id(resource_id, "pdb", strip=True, upper=True)
    if not _PDB_ID_RE.match(pid):
        raise NotFoundError(f"malformed PDB id {resource_id!r}")
    meta = await _hydrate(client, [pid])
    entry = meta.get(pid)
    if entry is None:
        raise NotFoundError(f"RCSB PDB has no entry {pid}")
    resource = _normalize(entry)
    # mmCIF exists for every entry. wwPDB makes a legacy PDB-format file only for entries
    # that fit that format (pdb_format_compatible "Y"); a large one (4V6X) has none, and
    # listing it made fetch 404 and fail as a whole.
    exts = ["cif"]
    if (entry.get("pdbx_database_status") or {}).get("pdb_format_compatible") == "Y":
        exts.append("pdb")
    files = [
        FileEntry(
            name=f"{pid}.{ext}",
            url=_DOWNLOAD.format(id=pid, ext=ext),
            mime="chemical/x-cif" if ext == "cif" else "chemical/x-pdb",
            source="rcsb",
        )
        for ext in exts
    ]
    return resource.model_copy(update={"files": files})
