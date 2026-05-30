"""ScholeXplorer (Scholix) link client — OpenAIRE's paper↔data link service.

Given a source DOI, returns the data resources it links to as ``Link``s whose
``target_id`` is in our canonical scheme (``datacite:<doi>``). Publication↔
publication citation edges (``target.Type == "literature"``) are dropped — that
is the standalone openalex MCP's job, not ours.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import Link

# v1 and v2 are being phased out; v3 (shape-identical) is the documented future.
SCHOLIX_VERSION = "v3"
BASE_URL = f"https://api.scholexplorer.openaire.eu/{SCHOLIX_VERSION}/Links"

# Scholix RelationshipType.Name → our model rel vocabulary.
_REL_MAP = {
    "issupplementedby": "is_supplement_to",
    "issupplementto": "is_supplement_to",
    "references": "references",
    "isreferencedby": "references",
    "isrelatedto": "is_related_to",
}
# target.Type values that are NOT data — dropped.
_DROP_TYPES = {"literature"}


def _map_rel(relationship: dict) -> str:
    name = (relationship.get("Name") or "").replace(" ", "").lower()
    return _REL_MAP.get(name, "is_related_to")


def _doi_of(identifiers: list[dict]) -> str | None:
    for ident in identifiers or []:
        if (ident.get("IDScheme") or "").lower() == "doi" and ident.get("ID"):
            return ident["ID"]
    return None


async def links_for(client: httpx.AsyncClient, doi: str | None) -> list[Link]:
    """Return data ``Link``s for the publication/dataset with ``doi``.

    No DOI → ``[]`` (cannot query without a source PID). 404 → ``[]``. Each
    non-literature target with a DOI becomes ``datacite:<doi>``.

    Reads only the first Scholix page: callers query publication source DOIs,
    where data targets are sparse (most edges are citations, which we drop), so
    a single page suffices.
    """
    if not doi:
        return []
    resp = await _http.request_with_retry(
        client,
        "GET",
        BASE_URL,
        service="ScholeXplorer",
        params={"sourcePid": doi},
        not_found_returns=None,
    )
    if resp is None:  # 404 — no entry for this PID
        return []
    out: list[Link] = []
    for rec in resp.json().get("result", []) or []:
        target = rec.get("target", {}) or {}
        if (target.get("Type") or "").lower() in _DROP_TYPES:
            continue
        target_doi = _doi_of(target.get("Identifier", []))
        if not target_doi:
            continue
        out.append(
            Link(rel=_map_rel(rec.get("RelationshipType", {})), target_id=f"datacite:{target_doi}")
        )
    return out
