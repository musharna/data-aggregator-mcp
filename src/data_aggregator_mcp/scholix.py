"""ScholeXplorer (Scholix) link client — OpenAIRE's paper↔data link service.

Given a source DOI, returns the data resources (``dataset`` / ``software`` targets) it
links to as ``Link``s. A target DataCite registered becomes ``datacite:<doi>``; any other
data DOI stays bare, claiming no registration agency. Every other target type — v3 calls
papers ``publication`` (v1/v2: ``literature``) — is a citation edge and is dropped; that
is the standalone openalex MCP's job, not ours.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError
from data_aggregator_mcp.models import Link

logger = logging.getLogger(__name__)

# v1 and v2 are being phased out; v3 (shape-identical) is the documented future.
SCHOLIX_VERSION = "v3"
BASE_URL = f"https://api.scholexplorer.openaire.eu/{SCHOLIX_VERSION}/Links"

# Scholix RelationshipType.Name → our model rel vocabulary (the DataCite snake-case
# names ``links`` uses everywhere else). The edge reads source → target, and the source
# is the DOI we queried, so each inverse pair keeps its own name: collapsing
# IsSupplementedBy onto is_supplement_to said "the paper supplements the dataset".
_REL_MAP = {
    "issupplementedby": "is_supplemented_by",
    "issupplementto": "is_supplement_to",
    "references": "references",
    "isreferencedby": "is_referenced_by",
    "isrelatedto": "is_related_to",
}
# target.Type values that ARE data — kept. An allow-list: the old deny-list named
# ``literature``, which v3 never emits (papers are ``publication``), so every citation
# edge leaked through as a "data link".
_DATA_TYPES = frozenset({"dataset", "software"})
# doi.org's registration-agency API; takes a comma-separated batch in one GET.
_RA_URL = "https://doi.org/ra/"
# Left unescaped in that batch: the path separator inside a DOI, and the batch commas.
_RA_SAFE = "/,"
_GET = "GET"


def _map_rel(relationship: dict) -> str:
    name = str(relationship.get("Name")).replace(" ", "").lower()
    return _REL_MAP.get(name, "is_related_to")


def _doi_of(identifiers: list[dict] | None) -> str | None:
    for ident in identifiers or []:
        if str(ident.get("IDScheme")).lower() == "doi" and ident.get("ID"):
            return ident["ID"]
    return None


def _is_link(rec: object) -> bool:
    """A link object whose fields ``links_for`` reads have the shape it reads them as:
    a ``target`` object, and, when present, a ``RelationshipType`` object and a list of
    identifier objects."""
    if not (isinstance(rec, dict) and isinstance(rec.get("target"), dict)):
        return False
    relationship = rec.get("RelationshipType")
    identifiers = rec["target"].get("Identifier")
    return (relationship is None or isinstance(relationship, dict)) and (
        identifiers is None
        or (isinstance(identifiers, list) and all(isinstance(i, dict) for i in identifiers))
    )


def _check_result(body: dict) -> None:
    """A Links search answers ``result``, empty when nothing links to the PID; each
    entry is a link object (``_is_link``)."""
    result = body.get("result")
    if not (isinstance(result, list) and all(_is_link(r) for r in result)):
        raise _http.UpstreamEnvelopeError(f"no result list of link objects in {body!r:.200}")


async def _datacite_registered(
    client: httpx.AsyncClient, dois: list[str]
) -> tuple[set[str], str | None]:
    """The (lower-cased) subset of ``dois`` whose registration agency is DataCite, and
    the lookup's failure reason (None when it answered).

    One batched doi.org RA lookup. A DOI containing a comma cannot be batched (the API
    splits on commas), so it is left out and stays a bare DOI. The lookup only decides
    the label: if it fails, every DOI stays bare (claiming no agency) and the links are
    still returned — a labelling outage must not sink the resolve they enrich — with the
    reason, so the record can say its labels are unchecked."""
    batch = [d for d in dict.fromkeys(dois) if "," not in d]
    if not batch:
        return set(), None
    try:
        rows = await _http.request_json(
            client,
            _GET,
            _RA_URL + quote(",".join(batch), safe=_RA_SAFE),
            service="DOI registration-agency lookup",
            expect=list,
        )
    except DataAggregatorError as exc:
        logger.warning(
            "doi.org RA lookup failed for %d DOI(s); links stay bare: %s", len(batch), exc
        )
        return set(), (
            f"doi.org registration-agency lookup failed ({type(exc).__name__}: {exc}); "
            f"{len(batch)} link(s) left as bare DOIs, not checked for DataCite"
        )
    return {
        str(r["DOI"]).lower()
        for r in rows
        if isinstance(r, dict) and r.get("RA") == "DataCite" and r.get("DOI")
    }, None


async def links_for(client: httpx.AsyncClient, doi: str | None) -> tuple[list[Link], str | None]:
    """Return data ``Link``s for the publication/dataset with ``doi``, and why they are
    incomplete (None when nothing failed).

    No DOI → ``[]`` (cannot query without a source PID). Each dataset/software target
    with a DOI becomes a Link: ``datacite:<doi>`` when DataCite registered it, else the
    bare DOI. A failed or off-contract Scholix answer, or a failed agency lookup,
    degrades (no links / bare DOIs) and is named in the reason: links are enrichment, so
    an outage must not sink the resolve, and must never read as "this record has no
    data links". ScholeXplorer answers a PID it has no links for with 200 and an empty
    ``result``, so a 404 is a failure like any other.

    Reads only the first Scholix page: callers query publication source DOIs,
    where data targets are sparse (most edges are citations, which we drop), so
    a single page suffices.
    """
    if not doi:
        return [], None
    try:
        payload = await _http.request_json(
            client,
            _GET,
            BASE_URL,
            service="ScholeXplorer",
            params={"sourcePid": doi},
            expect=dict,
            check=_check_result,
        )
    except DataAggregatorError as exc:
        logger.warning("ScholeXplorer lookup failed for %r: %s", doi, exc)
        return [], f"ScholeXplorer lookup failed ({type(exc).__name__}: {exc}); data links unknown"
    edges: list[tuple[str, str]] = []
    for rec in payload["result"]:
        target = rec["target"]
        if str(target.get("Type")).lower() not in _DATA_TYPES:
            continue
        target_doi = _doi_of(target.get("Identifier"))
        if not target_doi:
            continue
        edges.append((_map_rel(rec.get("RelationshipType") or {}), target_doi))
    datacite, error = await _datacite_registered(client, [d for _, d in edges])
    return [
        Link(rel=rel, target_id=f"datacite:{d}" if d.lower() in datacite else d) for rel, d in edges
    ], error
