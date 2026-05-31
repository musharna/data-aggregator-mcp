"""NCBI E-utilities plumbing (esearch + esummary + elink JSON; efetch XML/text).

Optional ``NCBI_API_KEY`` env var raises the NCBI rate limit (3→10 req/s) and is
appended automatically when present. Normalization lives in the adapters, not here.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from data_aggregator_mcp import _http

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


def _api_key_params() -> dict[str, str]:
    key = os.environ.get("NCBI_API_KEY")
    return {"api_key": key} if key else {}


def _common_params() -> dict[str, str]:
    return {"retmode": "json", **_api_key_params()}


async def esearch(
    client: httpx.AsyncClient,
    db: str,
    term: str,
    *,
    retmax: int,
    retstart: int = 0,
) -> tuple[int, list[str]]:
    """Return (total_count, idlist) for ``term`` in NCBI database ``db``."""
    params = {
        "db": db,
        "term": term,
        "retmax": str(retmax),
        **_common_params(),
    }
    if retstart:
        # only sent when paging past the first window, so the offset=0 request
        # stays byte-identical to the pre-pagination one (see P1 spec)
        params["retstart"] = str(retstart)
    data = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/esearch.fcgi",
        service=f"NCBI esearch ({db})",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = data.get("esearchresult", {}) or {}
    ids = result.get("idlist", []) or []
    count = int(result.get("count", len(ids)))
    return count, ids


async def esummary(
    client: httpx.AsyncClient,
    db: str,
    ids: list[str],
) -> list[dict[str, Any]]:
    """Return summary docs for ``ids`` (in idlist order). Empty ids → []."""
    if not ids:
        return []
    params = {"db": db, "id": ",".join(ids), "version": "2.0", **_common_params()}
    data = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/esummary.fcgi",
        service=f"NCBI esummary ({db})",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = data.get("result", {}) or {}
    uids = result.get("uids", []) or []
    return [result[u] for u in uids if u in result]


async def elink(
    client: httpx.AsyncClient,
    *,
    dbfrom: str,
    db: str,
    ids: list[str],
) -> list[str]:
    """Return target uids linking ``ids`` in ``dbfrom`` to records in ``db``.

    Flattens every ``linksetdbs[].links`` across all linksets. Empty ids or no
    edges → ``[]`` (a PMID with no link in ``db`` is normal, not an error).
    """
    if not ids:
        return []
    params = {"dbfrom": dbfrom, "db": db, "id": ",".join(ids), **_common_params()}
    data = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/elink.fcgi",
        service=f"NCBI elink ({dbfrom}->{db})",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    out: list[str] = []
    for linkset in data.get("linksets", []) or []:
        for linksetdb in linkset.get("linksetdbs", []) or []:
            out.extend(linksetdb.get("links", []) or [])
    return out


async def efetch(
    client: httpx.AsyncClient,
    db: str,
    ids: list[str],
    *,
    retmode: str = "xml",
) -> str:
    """Return the raw efetch body for ``ids`` in ``db``. Empty ids → ``""``.

    Unlike esearch/esummary this is NOT JSON mode — efetch serves XML/text;
    the caller parses. ``NCBI_API_KEY`` is honored when present.
    """
    if not ids:
        return ""
    params = {"db": db, "id": ",".join(ids), "retmode": retmode, **_api_key_params()}
    requester = _http.request_xml if retmode == "xml" else _http.request_with_retry
    resp = await requester(
        client,
        "GET",
        f"{BASE_URL}/efetch.fcgi",
        service=f"NCBI efetch ({db})",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    return resp.text
