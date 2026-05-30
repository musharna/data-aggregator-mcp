"""Open-access full-text discovery — EuropePMC XML → Unpaywall PDF cascade.

resolve() of a literature record attaches the first OA full-text file found and
the record's rights (access/license). EuropePMC fullTextXML (HTTPS, covers the
PMC OA subset) is tried first via a PMCID/DOI existence check (inEPMC=='Y'); its
``resultType=core`` record also carries ``license`` + ``isOpenAccess``. Unpaywall
(gated on UNPAYWALL_EMAIL) is the fallback for an OA PDF hosted elsewhere and
carries ``oa_status`` + ``best_oa_location.license``. Enrichment: any failure logs
a warning and the leg is skipped — never raises into a valid resolve (spec §8).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

logger = logging.getLogger(__name__)

EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


@dataclass(frozen=True)
class FullText:
    """OA full-text discovery result: the file (if any) + the record's rights."""

    file: FileEntry | None = None
    access: str | None = None
    license: str | None = None


async def _europepmc(client: httpx.AsyncClient, pmcid: str | None, doi: str | None) -> FullText:
    query = f"PMCID:{pmcid}" if pmcid else (f"DOI:{doi}" if doi else None)
    if not query:
        return FullText()
    try:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{EPMC_BASE}/search",
            service="EuropePMC search",
            params={"query": query, "format": "json", "resultType": "core", "pageSize": 1},
        )
        res = (resp.json().get("resultList", {}).get("result") or [{}])[0]
        if not isinstance(res, dict):
            return FullText()
        access = "open" if str(res.get("isOpenAccess", "")).upper() == "Y" else None
        license_ = res.get("license") or None
        if res.get("inEPMC") != "Y":
            return FullText(file=None, access=access, license=license_)
        epmc_pmcid = res.get("pmcid") or pmcid
        if not epmc_pmcid:
            return FullText(file=None, access=access, license=license_)
        url = f"{EPMC_BASE}/{epmc_pmcid}/fullTextXML"
        fe = FileEntry(
            name=f"{epmc_pmcid}.xml", mime="application/xml", url=url, source="europepmc"
        )
        return FullText(file=fe, access=access, license=license_)
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise (spec §8)
        logger.warning("EuropePMC lookup failed for %r: %r", pmcid or doi, exc)
        return FullText()


async def _unpaywall(client: httpx.AsyncClient, doi: str | None) -> FullText:
    if not doi:
        return FullText()
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        logger.warning("full text: UNPAYWALL_EMAIL unset; skipping Unpaywall leg for %r", doi)
        return FullText()
    try:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{UNPAYWALL_BASE}/{doi}",
            service="Unpaywall",
            params={"email": email},
            not_found_returns=None,
        )
        if resp is None:
            return FullText()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("is_oa"):
            return FullText()
        access = "open"
        loc = data.get("best_oa_location") or {}
        license_ = (loc.get("license") if isinstance(loc, dict) else None) or None
        pdf = loc.get("url_for_pdf") if isinstance(loc, dict) else None
        if not pdf:
            return FullText(file=None, access=access, license=license_)
        fe = FileEntry(name="fulltext.pdf", mime="application/pdf", url=pdf, source="unpaywall")
        return FullText(file=fe, access=access, license=license_)
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise (spec §8)
        logger.warning("Unpaywall lookup failed for %r: %r", doi, exc)
        return FullText()


async def find(
    client: httpx.AsyncClient, *, pmcid: str | None = None, doi: str | None = None
) -> FullText:
    """First OA full text (EuropePMC XML, then Unpaywall PDF) + the record's rights.
    Never raises — enrichment. Returns a FullText (``.file`` is None when no OA file)."""
    epmc = await _europepmc(client, pmcid, doi)
    if epmc.file is not None:
        return epmc
    upw = await _unpaywall(client, doi)
    if upw.file is not None:
        return upw
    return FullText(
        file=None,
        access=epmc.access or upw.access,
        license=epmc.license or upw.license,
    )
