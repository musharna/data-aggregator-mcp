"""Zenodo archives adapter — async wrapper around zenodo.org REST API.

Discovery: GET /api/records?q=...  |  Resolve: GET /api/records/{id}
Zenodo records carry a checksummed file manifest, so this single source
exercises the full search → resolve → fetch loop.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import httpx

from data_aggregator_mcp import _http, _pushdown
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    FundingRef,
    Link,
    Metrics,
    _orcid,
    _rel,
    compact,
    local_id,
    normalize_access,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://zenodo.org"
# Location of the /versions/latest redirect: the latest record's API url.
_RECORD_URL_RE = re.compile(r"^https://zenodo\.org/api/records/(\d+)$")
PREFIXES = frozenset({"zenodo"})  # bare-numeric ids also route here (see router.resolve)
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_SIZE = 10
MAX_SIZE = 50

# Search returns FULL records (manifest included); compact() strips files[] for the search
# view, so a naive search→resolve re-fetches what we already had. Stash the raw record here
# so resolve() can skip the redundant GET. Short TTL: resolve-after-search is near-immediate,
# and a longer window risks serving a stale manifest.
_SEARCH_CACHE: TTLCache = TTLCache(maxsize=256, ttl=600)

# Zenodo resource_type.type → DataResource.kind
_KIND_MAP = {
    "dataset": "dataset",
    "publication": "publication",
    "software": "software",
}


def _is_last_version(meta: dict[str, Any]) -> bool | None:
    """Zenodo's authoritative version-currency flag: ``metadata.relations.version[0].is_last``
    (Zenodo knows the whole version set of the concept). None when the record carries no
    version graph. The id of the newer version is NOT in the record (only a
    ``links.latest`` redirect) — resolve() follows that redirect for a non-latest record
    (``_latest_version_id``); search never does."""
    versions = (meta.get("relations") or {}).get("version") or []
    if not versions or not isinstance(versions[0], dict):
        return None
    is_last = versions[0].get("is_last")
    return is_last if isinstance(is_last, bool) else None


def _normalize(record: dict[str, Any]) -> DataResource:
    meta = record.get("metadata", {}) or {}
    rtype = (meta.get("resource_type") or {}).get("type")
    pub_date = meta.get("publication_date") or ""
    year = int(pub_date[:4]) if pub_date[:4].isdigit() else None
    files: list[FileEntry] = []
    for f in record.get("files", []) or []:
        links = f.get("links", {}) or {}
        files.append(
            FileEntry(
                name=f.get("key", ""),
                size=f.get("size"),
                url=links.get("self"),
                checksum=f.get("checksum"),
            )
        )
    stats = record.get("stats") or {}
    views, downloads = stats.get("views"), stats.get("downloads")
    metrics = (
        Metrics(
            views=int(views) if views is not None else None,
            downloads=int(downloads) if downloads is not None else None,
        )
        if (views is not None or downloads is not None)
        else None
    )
    return DataResource(
        id=f"zenodo:{record.get('id')}",
        source="zenodo",
        kind=_KIND_MAP.get(rtype or "", _pushdown.OTHER_KIND),
        title=meta.get("title", ""),
        creators=[
            Creator(name=c.get("name", ""), orcid=_orcid(c.get("orcid")))
            for c in meta.get("creators", []) or []
        ],
        funding=[
            FundingRef(funder=funder_name, award=g.get("code") or g.get("title"))
            for g in (meta.get("grants") or [])
            if (funder_name := (g.get("funder") or {}).get("name"))
        ],
        year=year,
        description=meta.get("description"),
        doi=record.get("doi"),
        subjects=list(meta.get("keywords", []) or []),
        license=(meta.get("license") or {}).get("id"),
        access=normalize_access(meta.get("access_right")),
        links=[
            Link(rel=_rel(r["relation"]), target_id=r["identifier"])
            for r in (meta.get("related_identifiers") or [])
            if r.get("relation") and r.get("identifier")
        ],
        files=files,
        metrics=metrics,
        is_latest=_is_last_version(meta),
    )


def _filter_clauses(filters: Mapping[str, Any]) -> list[str]:
    """Zenodo query clauses for the pushable facet filters. Dates are whole years, so the
    range spans Jan 1 of ``published_after`` to Dec 31 of ``published_before`` — the same
    bound ``_normalize``'s ``publication_date[:4]`` year is post-filtered against."""
    pa, pb = filters.get("published_after"), filters.get("published_before")
    clauses = [
        _pushdown.range_clause(
            "publication_date",
            f"{pa:04d}-01-01" if pa is not None else None,
            f"{pb:04d}-12-31" if pb is not None else None,
        )
    ]
    if (kind := filters.get("kind")) is not None:
        clauses.append(_pushdown.kind_clause("resource_type.type", _KIND_MAP, kind))
    return [c for c in clauses if c is not None]


def pushable(filters: Mapping[str, Any], /) -> dict[str, Any]:
    """The active filters Zenodo can evaluate: both year bounds, and any ``kind`` a
    Zenodo ``resource_type.type`` normalizes to (``_pushdown.FilterPushdown``)."""
    act = _pushdown.active(filters)
    out = {k: v for k, v in act.items() if k in _pushdown.YEAR_FILTERS}
    kind = act.get("kind")
    if kind is not None and _pushdown.kind_clause("resource_type.type", _KIND_MAP, kind):
        out["kind"] = kind
    return out


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    filters: Mapping[str, Any] | None = None,
) -> tuple[int, list[DataResource]]:
    """Search Zenodo records. Returns (total_hits, COMPACT resources).

    ``offset`` selects the window [offset, offset+size): request page
    ``offset // size + 1`` at page-size ``size`` and drop the first
    ``offset % size`` records (page-boundary slice; see pagination spec).

    ``filters`` (the subset :func:`pushable` accepted) are ANDed onto ``q``, so Zenodo
    evaluates them and ``total_hits`` is the filtered total. None/empty sends ``q``
    unchanged.
    """
    capped = min(size, MAX_SIZE)
    params = {
        "q": _pushdown.with_clauses(query, _filter_clauses(filters or {})),
        "size": str(capped),
    }
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page"] = str(offset // capped + 1)
    data = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/api/records",
        service="Zenodo search",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    hits = data.get("hits", {}) or {}
    records = hits.get("hits", []) or []
    sliced = records[offset % capped :]
    total = int(hits.get("total", len(records)))
    for r in sliced:  # stash raw records so resolve() can skip a redundant GET
        if r.get("id") is not None:
            _SEARCH_CACHE.set(f"zenodo:{r['id']}", r)
    return total, [compact(_normalize(r)) for r in sliced]


async def _latest_version_id(client: httpx.AsyncClient, rid: str) -> str | None:
    """``zenodo:<id>`` of the latest version of ``rid``'s concept, read from the 301
    Location of ``HEAD /api/records/{rid}/versions/latest`` (the redirect is not
    followed). None means ``rid`` is itself the latest; a failed lookup or an answer
    with no record id raises, so it is never read as "no newer version"."""
    url = f"{BASE_URL}/api/records/{rid}/versions/latest"
    resp = await _http.request_with_retry(
        client,
        "HEAD",
        url,
        service="Zenodo latest version",
        timeout=DEFAULT_TIMEOUT,
        max_retries=2,
        follow_redirects=False,
    )
    location = resp.headers.get("location") if resp.is_redirect else None
    m = _RECORD_URL_RE.match(urljoin(url, location)) if location else None
    if m is None:
        raise UpstreamUnavailableError(
            f"Zenodo latest-version lookup for {rid}: HTTP {resp.status_code}, "
            f"Location {location!r} — no record id"
        )
    return None if m.group(1) == rid else f"zenodo:{m.group(1)}"


async def _with_superseded_by(client: httpx.AsyncClient, rid: str, r: DataResource) -> DataResource:
    """Resolve-only: a record Zenodo says is NOT the last version gets superseded_by from
    one HEAD. Latest / unversioned records make no extra call. The lookup is enrichment:
    a failure leaves superseded_by unknown, is recorded in ``errors["superseded_by"]``
    (which also keeps the record out of the resolve cache), and the resolve succeeds."""
    if r.is_latest is not False:
        return r
    try:
        newer = await _latest_version_id(client, rid)
    except Exception as exc:  # noqa: BLE001 — enrichment: never sink a valid resolve
        logger.warning("zenodo latest-version lookup failed for %s: %r", rid, exc)
        return r.model_copy(
            update={"errors": {**r.errors, "superseded_by": f"{type(exc).__name__}: {exc}"}}
        )
    return r.model_copy(update={"superseded_by": newer}) if newer else r


async def resolve(client: httpx.AsyncClient, record_id: str) -> DataResource:
    """Resolve a Zenodo record by id (``zenodo:123`` or bare ``123``)."""
    rid = local_id(record_id, "zenodo")
    cached = _SEARCH_CACHE.get(f"zenodo:{rid}")
    if cached is not MISS:  # seeded by a recent search — full record already in hand
        return await _with_superseded_by(client, rid, _normalize(cached))
    try:
        record = await _http.request_json(
            client,
            "GET",
            f"{BASE_URL}/api/records/{rid}",
            service="Zenodo resolve",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"Zenodo has no record id={rid!r}") from None
    return await _with_superseded_by(client, rid, _normalize(record))
