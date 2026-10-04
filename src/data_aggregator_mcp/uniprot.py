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
from data_aggregator_mcp.models import DataResource, FileEntry, Link, compact, local_id

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
# search serves page 1 only (``if offset`` below), so list_sources omits ``cursor``.
PAGINATES = False
MAX_RETRIES = 2
# httpx upper-cases the method and reads header names case-insensitively, so neither
# spelling is behaviour.
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
_TOTAL_RESULTS = "x-total-results"  # the corpus hit count; the body carries only the page


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


def _curation(entry_type: str | None) -> str | None:
    if not entry_type:
        return None
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
    curation = _curation(entry.get("entryType"))
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
        identifiers=identifiers,
        subjects=subjects,
        last_updated=(entry.get("entryAudit") or {}).get("lastAnnotationUpdateDate"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(acc=acc))],
    )


def _inactive_message(acc: str, reason: dict) -> str:
    kind = reason.get("inactiveReasonType") or "unknown reason"
    msg = f"UniProtKB entry {acc} is inactive ({kind})"
    if reason.get("deletedReason"):
        msg += f": {reason['deletedReason']}"
    targets = reason.get("mergeDemergeTo") or []
    if targets:
        msg += "; now " + ", ".join(f"uniprot:{t}" for t in targets)
    return msg


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    if offset:
        return 0, []
    body, resp_headers = await _http.request_json_with_headers(
        client,
        _GET,
        SEARCH,
        service="UniProt search",
        params={"query": query, "format": "json", "size": min(size, MAX_SIZE)},
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        # No not_found_returns: no hits is a 200 with `results: []` (live, 2026-10-01),
        # so a 404 is not an empty search.
        expect=dict,
    )
    results = body.get("results") or []
    recs = [compact(_normalize(r)) for r in results]
    # Fall back to the page length if the hit count is absent.
    total_hdr = resp_headers.get(_TOTAL_RESULTS)
    total = int(total_hdr) if total_hdr and total_hdr.isdigit() else len(recs)
    return total, recs


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = local_id(resource_id, "uniprot", strip=True, upper=True)
    if not _ACC_RE.match(acc):
        raise NotFoundError(f"malformed UniProt id {resource_id!r}")
    try:
        body = await _http.request_json(
            client,
            _GET,
            ENTRY.format(acc=acc),
            service="UniProt resolve",
            params={"format": "json"},
            headers=_ACCEPT_JSON,
            max_retries=MAX_RETRIES,
            expect=dict,
        )
    except NotFoundError:
        raise NotFoundError(f"UniProtKB has no entry {acc}") from None
    if body.get("entryType") == "Inactive":
        # A deleted/merged accession is answered with HTTP 200 and a stub carrying no
        # sequence (its .fasta is empty). It is not a live entry: say why, don't normalise.
        raise NotFoundError(_inactive_message(acc, body.get("inactiveReason") or {}))
    resource = _normalize(body)
    fasta = FileEntry(
        name=f"{acc}.fasta",
        url=_FASTA.format(acc=acc),
        mime="text/x-fasta",
        source="uniprot",
    )
    return resource.model_copy(update={"files": [fasta]})
