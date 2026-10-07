"""NCBI E-utilities plumbing (esearch + esummary + elink JSON; efetch XML/text).

Optional ``NCBI_API_KEY`` env var raises the NCBI rate limit (3→10 req/s; ``_ratelimit``
paces at two thirds of it, machine-wide) and is appended automatically when present.
Normalization lives in the adapters, not here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, TypeGuard

import httpx

from data_aggregator_mcp import _http

logger = logging.getLogger(__name__)

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# httpx upper-cases the method, so a spelling mutant of it sends the same request
# (test__eutils_observed.py pins the GET).
_GET = "GET"


def _api_key_params() -> dict[str, str]:
    key = os.environ.get("NCBI_API_KEY")
    return {"api_key": key} if key else {}


def _common_params() -> dict[str, str]:
    return {"retmode": "json", **_api_key_params()}


def _strs(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_esearch(body: dict) -> None:
    """NCBI reports a failed search INSIDE a 200: ``esearchresult.ERROR``. Read as
    count 0, an outage looked like "no records" and taxonomy negative-cached it for an
    hour. A genuine no-match carries ``count: "0"`` plus warninglist/errorlist entries
    (phrasesnotfound) and no ``ERROR`` key, so it still passes. Anything else without
    a decimal-string ``count`` and a list of string ids is not a search answer."""
    result = body.get("esearchresult")
    result = result if isinstance(result, dict) else {}
    if result.get("ERROR"):
        raise _http.UpstreamEnvelopeError(f"NCBI esearch ERROR: {result['ERROR']}")
    if isinstance(body.get("error"), str):
        raise _http.UpstreamEnvelopeError(f"NCBI esearch error: {body['error']}")
    count = result.get("count")
    if not (isinstance(count, str) and count.isdecimal() and _strs(result.get("idlist"))):
        raise _http.UpstreamEnvelopeError(f"no NCBI esearch result in {body!r:.200}")


def _check_esummary(body: dict) -> None:
    """A top-level ``error`` is a failed call. (A PER-UID ``error`` is not: it means
    that uid has no record, and ``esummary`` drops it.) A failure NCBI reports as
    ``esummaryresult`` (bad db, empty id list) has no ``result`` at all; read as an
    empty result, it made every requested uid "not found". Every listed uid must
    have its summary object."""
    if isinstance(body.get("error"), str):
        raise _http.UpstreamEnvelopeError(f"NCBI esummary error: {body['error']}")
    result = body.get("result")
    result = result if isinstance(result, dict) else {}
    uids = result.get("uids")
    if not (_strs(uids) and all(isinstance(result.get(u), dict) for u in uids)):
        raise _http.UpstreamEnvelopeError(f"no NCBI esummary result in {body!r:.200}")


def _is_linkset(linkset: object) -> bool:
    dbs = linkset.get("linksetdbs", []) if isinstance(linkset, dict) else None
    return isinstance(dbs, list) and all(
        isinstance(d, dict) and _strs(d.get("links", [])) for d in dbs
    )


def _check_elink(body: dict) -> None:
    """elink reports a failed call as a top-level ``ERROR`` next to an empty
    ``linksets`` (live 2026-10-02: an invalid db); read as "no links", a failure
    looked like a record with no data. ``linksetdbs``/``links`` may be absent (no
    edge), never another type."""
    if isinstance(body.get("ERROR"), str):
        raise _http.UpstreamEnvelopeError(f"NCBI elink ERROR: {body['ERROR']}")
    linksets = body.get("linksets")
    if not (isinstance(linksets, list) and all(_is_linkset(s) for s in linksets)):
        raise _http.UpstreamEnvelopeError(f"no NCBI elink linksets in {body!r:.200}")


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
        _GET,
        f"{BASE_URL}/esearch.fcgi",
        service=f"NCBI esearch ({db})",
        params=params,
        check=_check_esearch,
        expect=dict,
    )
    result = data["esearchresult"]
    return int(result["count"]), result["idlist"]


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
        _GET,
        f"{BASE_URL}/esummary.fcgi",
        service=f"NCBI esummary ({db})",
        params=params,
        check=_check_esummary,
        expect=dict,
    )
    result = data["result"]
    docs: list[dict[str, Any]] = []
    for u in result["uids"]:
        doc = result[u]
        if doc.get("error"):
            # Per-uid "cannot get document summary": this uid has no record. Kept, it
            # normalised into an empty success-shaped record (a bad PMID "resolved").
            logger.info("NCBI esummary (%s): uid %s has no record: %s", db, u, doc["error"])
            continue
        docs.append(doc)
    return docs


async def elink(
    client: httpx.AsyncClient,
    *,
    dbfrom: str,
    db: str,
    ids: list[str],
) -> list[str]:
    """Return target uids linking ``ids`` in ``dbfrom`` to records in ``db``.

    The union of every ``linksetdbs[].links`` across all linksets, in first-seen order:
    NCBI can answer one request with overlapping linksets (bioproject->sra returns
    ``bioproject_sra`` and ``bioproject_sra_all`` with the same runs), so a flat concat
    listed each uid twice. Empty ids or no edges → ``[]`` (a PMID with no link in ``db``
    is normal, not an error).
    """
    if not ids:
        return []
    params = {"dbfrom": dbfrom, "db": db, "id": ",".join(ids), **_common_params()}
    data = await _http.request_json(
        client,
        _GET,
        f"{BASE_URL}/elink.fcgi",
        service=f"NCBI elink ({dbfrom}->{db})",
        params=params,
        check=_check_elink,
        expect=dict,
    )
    out: dict[str, None] = {}
    for linkset in data["linksets"]:
        for linksetdb in linkset.get("linksetdbs", []):
            out.update(dict.fromkeys(linksetdb.get("links", [])))
    return list(out)


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
        _GET,
        f"{BASE_URL}/efetch.fcgi",
        service=f"NCBI efetch ({db})",
        params=params,
    )
    return resp.text
