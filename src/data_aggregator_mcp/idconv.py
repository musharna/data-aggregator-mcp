"""NCBI ID Converter — map a DOI to the {doi, pmid, pmcid} triad.

A single GET to www.ncbi.nlm.nih.gov/pmc/utils/idconv (NOT the eutils host).
Used by the literature adapters to populate DataResource.identifiers. Best
effort: never raises (spec §8), but a failure is returned as a reason next to the
empty result, so it is never indistinguishable from "this DOI is not in PMC".
"""

from __future__ import annotations

import logging
import os

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
TOOL = "data-aggregator-mcp"
_GET = "GET"


def _check_records(body: dict) -> None:
    """idconv answers every id with a record, a DOI it cannot convert included
    (``status: "error"``), so a body without one is not an answer."""
    records = body.get("records")
    if not (isinstance(records, list) and records and isinstance(records[0], dict)):
        raise _http.UpstreamEnvelopeError(f"no records list of objects in {body!r:.200}")


async def identifiers_for(
    client: httpx.AsyncClient, doi: str | None
) -> tuple[dict[str, str], str | None]:
    """Resolve ``doi`` to {doi, pmid, pmcid} (missing ids omitted), and why that is
    incomplete (None when nothing failed). Empty doi or a DOI idconv reports as not in
    PMC → ``({}, None)``; a failed or off-contract lookup → ``({}, reason)``."""
    if not doi:
        return {}, None
    params = {"ids": doi, "format": "json", "tool": TOOL}
    email = os.environ.get("NCBI_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    if email:
        params["email"] = email
    try:
        body = await _http.request_json(
            client,
            _GET,
            BASE_URL,
            service="NCBI idconv",
            params=params,
            expect=dict,
            check=_check_records,
        )
    except DataAggregatorError as exc:  # enrichment: degrade with the reason (spec §8)
        logger.warning("idconv failed for %r: %r", doi, exc)
        return {}, f"NCBI idconv lookup failed: {type(exc).__name__}: {exc}"
    rec = body["records"][0]
    if rec.get("status") == "error":  # idconv's answer for a DOI that is not in PMC
        return {}, None
    out: dict[str, str] = {}
    for key in ("doi", "pmid", "pmcid"):
        val = rec.get(key)
        if val:
            out[key] = str(val)
    return out, None
