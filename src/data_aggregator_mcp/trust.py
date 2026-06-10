"""Trust / integrity signals — retraction status via Crossref.

resolve(trust=True) calls annotate() to attach a TrustSignals to a resolved
resource. The retraction check is ONE Crossref /works/{doi} call: a retracted
work carries its retraction under message.updated-by[] with type=="retraction"
(verified live on real retracted DOIs 2026-06-10 — NOT update-to[], which is the
retraction notice's inverse view). A DOI Crossref doesn't register (e.g. a DataCite
data DOI) 404s → all fields stay None (unknown, NOT a false "clean" claim). Only the
404 path is benign; any other HTTP/parse error fails loud through _http.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource, TrustSignals

CROSSREF = "https://api.crossref.org/works/{doi}"
_RETRACTION = "retraction"
_CONCERN = "expression_of_concern"
# Crossref polite-pool etiquette: identify the client (no personal email).
_HEADERS = {
    "User-Agent": "data-aggregator-mcp (+https://github.com/musharna/data-aggregator-mcp)",
    "Accept": "application/json",
}
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _doi_of(resource: DataResource) -> str | None:
    return resource.doi or resource.identifiers.get("doi")


async def annotate(client: httpx.AsyncClient, resource: DataResource) -> TrustSignals:
    doi = _doi_of(resource)
    if not doi:
        return TrustSignals()  # nothing to check → unknown
    body = await _http.request_json(
        client,
        "GET",
        CROSSREF.format(doi=quote(doi, safe="/")),
        service="Crossref retraction",
        headers=_HEADERS,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,  # 404 → not a Crossref work → unknown
    )
    if not isinstance(body, dict):
        return TrustSignals()
    updated_by = (body.get("message") or {}).get("updated-by") or []
    notice = next(
        (u for u in updated_by if isinstance(u, dict) and u.get("type") == _RETRACTION),
        None,
    )
    concern = any(isinstance(u, dict) and u.get("type") == _CONCERN for u in updated_by)
    return TrustSignals(
        retracted=notice is not None,
        retraction_doi=notice.get("DOI") if notice else None,
        concern=concern,
    )
