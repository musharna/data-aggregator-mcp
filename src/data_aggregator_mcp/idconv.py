"""NCBI ID Converter — map a DOI to the {doi, pmid, pmcid} triad.

A single GET to www.ncbi.nlm.nih.gov/pmc/utils/idconv (NOT the eutils host).
Used by the literature adapters to populate DataResource.identifiers. Best
effort: any failure logs a warning and returns {} — never raises (spec §8).
"""

from __future__ import annotations

import logging
import os

import httpx

from data_aggregator_mcp import _http

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
TOOL = "data-aggregator-mcp"


async def identifiers_for(client: httpx.AsyncClient, doi: str | None) -> dict[str, str]:
    """Resolve ``doi`` to {doi, pmid, pmcid} (missing ids omitted). Empty doi or
    any failure → {}."""
    if not doi:
        return {}
    params = {"ids": doi, "format": "json", "tool": TOOL}
    email = os.environ.get("NCBI_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    if email:
        params["email"] = email
    try:
        resp = await _http.request_with_retry(
            client, "GET", BASE_URL, service="NCBI idconv", params=params
        )
        rec = (resp.json().get("records") or [{}])[0]
        if not isinstance(rec, dict) or rec.get("status") == "error":
            return {}
        out: dict[str, str] = {}
        for key in ("doi", "pmid", "pmcid"):
            val = rec.get(key)
            if val:
                out[key] = str(val)
        return out
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise (spec §8)
        logger.warning("idconv failed for %r: %r", doi, exc)
        return {}
