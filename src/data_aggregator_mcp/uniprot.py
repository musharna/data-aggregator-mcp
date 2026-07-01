"""UniProtKB — protein sequences and functional annotation.

Full-text search returns entry records (accession, protein name, organism,
curation status); the sequence itself streams from the .fasta endpoint with no
upstream checksum -> fetch is unverified, not operable. UniProt paginates by
Link-header cursor rather than row offset, so (like huggingface) this
contributes to page 1 only — offset>0 returns no rows. kind="dataset".
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, FileEntry, Link, compact

SEARCH = "https://rest.uniprot.org/uniprotkb/search"
ENTRY = "https://rest.uniprot.org/uniprotkb/{acc}"
_FASTA = "https://rest.uniprot.org/uniprotkb/{acc}.fasta"
_LANDING = "https://www.uniprot.org/uniprotkb/{acc}/entry"
PREFIXES = {"uniprot"}
# UniProt accessions are 6 or 10 alnum chars; entry names (INS_HUMAN) add an
# underscore. This charset also guards resolve's user-supplied id from path
# traversal / injection before it reaches the URL.
_ACC_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DEFAULT_SIZE = 10
MAX_SIZE = 25
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _protein_name(entry: dict) -> str:
    desc = entry.get("proteinDescription") or {}
    rec = ((desc.get("recommendedName") or {}).get("fullName") or {}).get("value")
    if rec:
        return rec
    subs = desc.get("submissionNames") or []
    if subs:
        sub = ((subs[0] or {}).get("fullName") or {}).get("value")
        if sub:
            return sub
    return entry.get("uniProtkbId") or entry.get("primaryAccession") or ""


def _curation(entry_type: str) -> str | None:
    if "Swiss-Prot" in entry_type:
        return "Swiss-Prot"
    if "TrEMBL" in entry_type:
        return "TrEMBL"
    return None


def _normalize(entry: dict) -> DataResource:
    acc = entry["primaryAccession"]
    organism = (entry.get("organism") or {}).get("scientificName")
    taxid = (entry.get("organism") or {}).get("taxonId")
    genes = entry.get("genes") or []
    gene = ((genes[0] or {}).get("geneName") or {}).get("value") if genes else None
    curation = _curation(entry.get("entryType") or "")
    identifiers: dict[str, str] = {}
    if taxid:
        identifiers["taxid"] = str(taxid)
    if gene:
        identifiers["gene"] = gene
    subjects = [s for s in (organism, curation) if s]
    return DataResource(
        id=f"uniprot:{acc}",
        source="uniprot",
        kind="dataset",
        title=_protein_name(entry),
        doi=None,
        identifiers=identifiers,
        subjects=subjects,
        last_updated=(entry.get("entryAudit") or {}).get("lastAnnotationUpdateDate"),
        files=[],
        links=[Link(rel="landing_page", target_id=_LANDING.format(acc=acc))],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    if offset:
        return 0, []
    resp = await _http.request_with_retry(
        client,
        "GET",
        SEARCH,
        service="UniProt search",
        params={"query": query, "format": "json", "size": min(size, MAX_SIZE)},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if resp is None:
        return 0, []
    body = resp.json()
    results = (body or {}).get("results") or []
    recs = [compact(_normalize(r)) for r in results]
    # x-total-results is the true corpus hit count; fall back to page size if absent.
    total_hdr = resp.headers.get("x-total-results")
    total = int(total_hdr) if total_hdr and total_hdr.isdigit() else len(recs)
    return total, recs


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = resource_id.split(":", 1)[1].strip().upper() if ":" in resource_id else ""
    if not _ACC_RE.match(acc):
        raise NotFoundError(f"malformed UniProt id {resource_id!r}")
    try:
        body = await _http.request_json(
            client,
            "GET",
            ENTRY.format(acc=acc),
            service="UniProt resolve",
            params={"format": "json"},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"UniProtKB has no entry {acc}") from None
    resource = _normalize(body)
    fasta = FileEntry(
        name=f"{acc}.fasta",
        url=_FASTA.format(acc=acc),
        mime="text/x-fasta",
        source="uniprot",
    )
    return resource.model_copy(update={"files": [fasta]})
