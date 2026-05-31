"""Citation rendering for resolved records — DOI content negotiation + CSL-JSON fallback.

DOI-bearing records render via DOI content negotiation (GET https://doi.org/<doi>, the
CrossCite mechanism covering CrossRef + DataCite); no CSL engine is bundled. Non-DOI
records still produce CSL-JSON from our normalized metadata. This is enrichment: any
failure logs a warning and returns None — it never raises into a valid resolve (spec §8).
"""

from __future__ import annotations

import json
import logging

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource

logger = logging.getLogger(__name__)

DOI_BASE = "https://doi.org"

# Structured formats with a dedicated content-negotiation MIME; any other value is
# treated as a CSL style name rendered as a text bibliography (apa, mla, vancouver, ...).
_FORMAT_ACCEPT = {
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
    "csl-json": "application/vnd.citationstyles.csl+json",
}

# DataResource.kind -> CSL-JSON type
_CSL_TYPE = {
    "publication": "article-journal",
    "dataset": "dataset",
    "software": "software",
    "study": "dataset",
    "sequencing_run": "dataset",
}


def _accept_for(fmt: str) -> str:
    return _FORMAT_ACCEPT.get(fmt) or f"text/x-bibliography; style={fmt}"


def _csl_json_from_metadata(r: DataResource) -> str:
    item: dict = {"id": r.id, "type": _CSL_TYPE.get(r.kind, "dataset"), "title": r.title}
    if r.creators:
        item["author"] = [{"literal": c.name} for c in r.creators]
    if r.year:
        item["issued"] = {"date-parts": [[r.year]]}
    if r.doi:
        item["DOI"] = r.doi
    return json.dumps(item)


async def render(client: httpx.AsyncClient, resource: DataResource, fmt: str) -> str | None:
    """Render a citation for ``resource`` in ``fmt``. DOI records use DOI content
    negotiation; non-DOI records yield CSL-JSON from metadata only. Fail soft: any
    failure logs a warning and returns None — this enrichment never raises (spec §8)."""
    fmt = (fmt or "").strip().lower()
    if not fmt:
        return None
    try:
        if not resource.doi:
            if fmt == "csl-json":
                return _csl_json_from_metadata(resource)
            logger.warning("citation: format %r needs a DOI; %s has none", fmt, resource.id)
            return None
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{DOI_BASE}/{resource.doi}",
            service="DOI content negotiation",
            headers={"Accept": _accept_for(fmt)},
        )
        return resp.text.strip() or None
    except Exception as exc:  # noqa: BLE001 — enrichment contract: never raise (spec §8)
        logger.warning("citation render failed for %s (%s): %r", resource.id, fmt, exc)
        return None
