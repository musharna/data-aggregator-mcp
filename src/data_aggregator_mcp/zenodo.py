"""Zenodo archives adapter — async wrapper around zenodo.org REST API.

Discovery: GET /api/records?q=...  |  Resolve: GET /api/records/{id}
Zenodo records carry a checksummed file manifest, so this single source
exercises the full search → resolve → fetch loop.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin

import httpx

from data_aggregator_mcp import _http, _pushdown
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp._relevance import with_plurals
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
    year_from,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://zenodo.org"
# Location of the /versions/latest redirect: the latest record's API url.
_RECORD_URL_RE = re.compile(r"^https://zenodo\.org/api/records/(\d+)$")
_RECORD_ID_RE = re.compile(r"[0-9]+")
PREFIXES = frozenset({"zenodo"})  # bare-numeric ids also route here (see router.resolve)
DEFAULT_SIZE = 10
# Zenodo refuses an anonymous page over 25 records: "400 Page size cannot be greater
# than 25. Please use authenticated requests to increase the limit to 100." (live
# 2026-10-02). At 50 every search asking for 26-50 records failed. The router pages each
# source in its own coordinates, so a source returning fewer than asked is fine.
MAX_SIZE = 25
# Matches words exactly, so search sends each word with its plural (``plurals``): "snow leopard"
# 11,734 hits, "snow leopards" 10,742, "snow (leopard OR leopards)" 11,925 (probed
# 2026-10-05).
QUERY_PLURALS = True
# The title field of Zenodo's query language; the router asks for title matches first.
# For "snow leopard": 56 of 12,040 hits hold it in a title, and 56 + 11,984 with
# "AND NOT" the clause = 12,040 (probed 2026-10-06).
TITLE_FIELD = "title"

# Search returns FULL records (manifest included); compact() strips files[] for the search
# view, so a naive search→resolve re-fetches what we already had. Stash the raw record here
# so resolve() can skip the redundant GET. Short TTL: resolve-after-search is near-immediate,
# and a longer window risks serving a stale manifest.
_SEARCH_CACHE: TTLCache = TTLCache(maxsize=256, ttl=600)

# httpx upper-cases the method and reads header names case-insensitively, so a
# spelling mutant of either sends the same request.
_GET = "GET"
_HEAD = "HEAD"
_ACCEPT_JSON = {"Accept": "application/json"}
# The field a kind filter targets; ``pushable`` only asks whether a clause exists.
_KIND_FIELD = "resource_type.type"

# Zenodo resource_type.type → DataResource.kind
_KIND_MAP = {
    "dataset": "dataset",
    "publication": "publication",
    "software": "software",
}
# The records the router ranks first among title matches: the types read as datasets.
DEPOSIT_CLAUSE = _pushdown.kind_clause(_KIND_FIELD, _KIND_MAP, "dataset", quoted=True)


def _opt(value: object, kind: type) -> bool:
    """Absent or null, or a ``kind`` (``True`` is not an ``int``)."""
    return value is None or (type(value) is int if kind is int else isinstance(value, kind))


def _fields(item: object, kinds: Mapping[str, type]) -> bool:
    return isinstance(item, dict) and all(_opt(item.get(k), t) for k, t in kinds.items())


def _items(value: object, ok: Callable[[Any], bool]) -> bool:
    return value is None or (isinstance(value, list) and all(ok(v) for v in value))


_META_FIELDS = {
    "title": str,
    "publication_date": str,
    "description": str,
    "resource_type": dict,
    "license": dict,
    "access_right": str,
    "relations": dict,
}


def _is_record(r: object) -> bool:
    """An int id, and every field ``_normalize`` reads at the type it reads it as
    (absent or null is fine)."""
    if not (isinstance(r, dict) and type(r.get("id")) is int and _opt(r.get("doi"), str)):
        return False
    meta, stats = r.get("metadata"), r.get("stats") or {}
    if not (isinstance(meta, dict) and _fields(meta, _META_FIELDS) and isinstance(stats, dict)):
        return False
    rtype, lic, rel = (
        meta.get("resource_type") or {},
        meta.get("license") or {},
        meta.get("relations") or {},
    )
    return (
        _items(
            r.get("files"),
            lambda f: (
                _fields(f, {"key": str, "size": int, "checksum": str, "links": dict})
                and _opt((f.get("links") or {}).get("self"), str)
            ),
        )
        and _fields(stats, {"views": int, "downloads": int})
        and _opt(rtype.get("type"), str)
        and _opt(lic.get("id"), str)
        and _items(meta.get("creators"), lambda c: _fields(c, {"name": str, "orcid": str}))
        and _items(
            meta.get("grants"),
            lambda g: (
                _fields(g, {"code": str, "title": str, "funder": dict})
                and _opt((g.get("funder") or {}).get("name"), str)
            ),
        )
        and _items(meta.get("keywords"), lambda k: isinstance(k, str))
        and _items(
            meta.get("related_identifiers"),
            lambda x: _fields(x, {"relation": str, "identifier": str}),
        )
        and _items(rel.get("version"), lambda v: _fields(v, {"is_last": bool}))
    )


def _check_record(body: dict) -> None:
    if not _is_record(body):
        raise _http.UpstreamEnvelopeError(f"no Zenodo record in {body!r:.200}")


def _check_hits(body: dict) -> None:
    hits = body.get("hits")
    records = hits.get("hits") if isinstance(hits, dict) else None
    total = hits.get("total") if isinstance(hits, dict) else None
    if not (
        isinstance(records, list) and all(_is_record(r) for r in records) and type(total) is int
    ):
        raise _http.UpstreamEnvelopeError(f"no Zenodo record list in {body!r:.200}")


def _is_last_version(meta: dict[str, Any]) -> bool | None:
    """Zenodo's authoritative version-currency flag: ``metadata.relations.version[0].is_last``
    (Zenodo knows the whole version set of the concept). None when the record carries no
    version graph. The id of the newer version is NOT in the record (only a
    ``links.latest`` redirect) — resolve() follows that redirect for a non-latest record
    (``_latest_version_id``); search never does."""
    versions = (meta.get("relations") or {}).get("version") or []
    return versions[0].get("is_last") if versions else None


def _normalize(record: dict[str, Any]) -> DataResource:
    meta = record["metadata"]
    rtype = (meta.get("resource_type") or {}).get("type")
    files: list[FileEntry] = []
    for f in record.get("files") or []:
        links = f.get("links") or {}
        files.append(
            FileEntry(
                name=f.get("key") or "",
                size=f.get("size"),
                url=links.get("self"),
                checksum=f.get("checksum"),
            )
        )
    stats = record.get("stats") or {}
    views, downloads = stats.get("views"), stats.get("downloads")
    metrics = (
        Metrics(views=views, downloads=downloads)
        if (views is not None or downloads is not None)
        else None
    )
    return DataResource(
        id=f"zenodo:{record['id']}",
        source="zenodo",
        kind=_KIND_MAP.get(str(rtype), _pushdown.OTHER_KIND),
        title=meta.get("title") or "",
        creators=[
            Creator(name=c.get("name") or "", orcid=_orcid(c.get("orcid")))
            for c in meta.get("creators") or []
        ],
        funding=[
            FundingRef(funder=funder_name, award=g.get("code") or g.get("title"))
            for g in (meta.get("grants") or [])
            if (funder_name := (g.get("funder") or {}).get("name"))
        ],
        year=year_from(meta.get("publication_date")),
        description=meta.get("description"),
        doi=record.get("doi"),
        subjects=list(meta.get("keywords") or []),
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
        clauses.append(_pushdown.kind_clause(_KIND_FIELD, _KIND_MAP, kind))
    return [c for c in clauses if c is not None]


def pushable(filters: Mapping[str, Any], /) -> dict[str, Any]:
    """The active filters Zenodo can evaluate: both year bounds, and any ``kind`` a
    Zenodo ``resource_type.type`` normalizes to (``_pushdown.FilterPushdown``)."""
    act = _pushdown.active(filters)
    out = {k: v for k, v in act.items() if k in _pushdown.YEAR_FILTERS}
    kind = act.get("kind")
    if kind is not None and _filter_clauses({"kind": kind}):
        out["kind"] = kind
    return out


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    filters: Mapping[str, Any] | None = None,
    plurals: bool = True,
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
        "q": _pushdown.with_clauses(
            with_plurals(query) if plurals else query, _filter_clauses(filters or {})
        ),
        "size": str(capped),
    }
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page"] = str(offset // capped + 1)
    data = await _http.request_json(
        client,
        _GET,
        f"{BASE_URL}/api/records",
        service="Zenodo search",
        params=params,
        headers=_ACCEPT_JSON,
        expect=dict,
        check=_check_hits,
    )
    sliced = data["hits"]["hits"][offset % capped :]
    total = data["hits"]["total"]
    for r in sliced:  # stash raw records so resolve() can skip a redundant GET
        _SEARCH_CACHE.set(f"zenodo:{r['id']}", r)
    return total, [compact(_normalize(r)) for r in sliced]


async def _latest_version_id(client: httpx.AsyncClient, rid: str) -> str | None:
    """``zenodo:<id>`` of the latest version of ``rid``'s concept, read from the 301
    Location of ``HEAD /api/records/{rid}/versions/latest`` (the redirect is not
    followed). None means ``rid`` is itself the latest; a failed lookup or an answer
    with no record id raises, so it is never read as "no newer version"."""
    url = f"{BASE_URL}/api/records/{rid}/versions/latest"
    # follow_redirects=None reads as "don't follow" in _http and httpx alike, and mutmut
    # ignores a pragma inside a call, so the call is exempt whole. Its arguments are
    # pinned by test_zenodo_observed: the method and URL, the 2 tries, the service name,
    # and False by the 301 being read rather than followed.
    # pragma: no mutate start
    resp = await _http.request_with_retry(
        client,
        _HEAD,
        url,
        service="Zenodo latest version",
        max_retries=2,
        follow_redirects=False,
    )
    # pragma: no mutate end
    # Header names are case-insensitive, so a spelling mutant reads the same header.
    location = resp.headers.get("location") if resp.is_redirect else None  # pragma: no mutate
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
    # A record id is digits, and it goes into the URL path: anything else
    # (`../../x?y=1`) would reach another endpoint.
    if not _RECORD_ID_RE.fullmatch(rid):
        raise NotFoundError(f"malformed Zenodo id {record_id!r}")
    cached = _SEARCH_CACHE.get(f"zenodo:{rid}")
    if cached is not MISS:  # seeded by a recent search — full record already in hand
        return await _with_superseded_by(client, rid, _normalize(cached))
    try:
        record = await _http.request_json(
            client,
            _GET,
            f"{BASE_URL}/api/records/{rid}",
            service="Zenodo resolve",
            headers=_ACCEPT_JSON,
            expect=dict,
            check=_check_record,
        )
    except NotFoundError:
        raise NotFoundError(f"Zenodo has no record id={rid!r}") from None
    return await _with_superseded_by(client, rid, _normalize(record))
