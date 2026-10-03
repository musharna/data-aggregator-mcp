"""data.gov (US government open-data catalog) — dataset search + resolve + fetch.

The unit is a DCAT-US dataset in the data.gov Catalog, the Flask/OpenSearch service that
replaced the CKAN catalog in 2026 (the old ``api.gsa.gov/.../v3/action/package_*`` CKAN
routes now redirect to a 404). Its OpenAPI spec is ``https://catalog.data.gov/openapi.json``.
Each search hit carries catalog fields (``slug``, ``organization``, ...) plus the
dataset's DCAT-US record under ``dcat`` (title, description, publisher, keyword, theme,
issued/modified, license, accessLevel, distribution[]).

**Auth.** The catalog's own API (``catalog.data.gov``: ``/search``,
``/api/dataset/{slug_or_id}``) needs no key; that is the default route. When
``DATA_GOV_API_KEY`` is set the same API is reached through the api.data.gov gateway
(``api.gsa.gov/technology/datagov/v4``: ``/search``, ``/dataset/{slug_or_id}``) with the
key in ``X-Api-Key`` — the documented keyed route (1,000 requests/hour per key). The
gateway's shared ``DEMO_KEY`` is never used: it allowed 10 requests/hour on 2026-09-27,
then answered 429 with a ~6-hour Retry-After.

**Pagination.** ``/search`` pages by an opaque ``after`` cursor, with no numeric offset
and no hit count. The router's per-stream offset is served by walking the cursor from
the start (one request while ``offset + size`` fits one ``_PAGE_MAX`` page), so a router
cursor replays exactly. The returned total is a LOWER BOUND: the records seen so far,
plus one when upstream reports more (``after`` present). That keeps the router's "more
pages" signal correct; it is not the number of matching datasets.

**Fetch is unverified.** DCAT distributions publish no checksum or size, so
``FileEntry.checksum`` is None and fetch runs unverified. Each file carries the
distribution's ``mediaType``, so the HTML sniff in ``fetch.py`` still rejects an HTML
page served in place of a declared PDF / XML file. A dataset with no distribution URL is
discovery-only and fails loud on fetch.

Most data.gov datasets carry no DOI, so DOI-dedup rarely fires here — the value is
breadth (government / economic / climate / civic data), not cross-source collapse.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    compact,
    local_id,
    strip_html,
    year_from,
)

CATALOG = "https://catalog.data.gov"
GATEWAY = "https://api.gsa.gov/technology/datagov/v4"
PREFIXES = {"datagov"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
_PAGE_MAX = 1000  # upstream per_page ceiling (OpenAPI: 1 <= per_page <= 1000)
# httpx upper-cases the method and reads header names case-insensitively, so a
# spelling mutant of either sends the same request.
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
_KEY_HEADER = "X-Api-Key"
# A slug or UUID is one path segment: quote() escapes "/" too.
_SEGMENT_SAFE = ""

# Licence URLs of the form http://www.opendefinition.org/licenses/<id> end in a CKAN
# license_id that normalize_spdx does not recognize; these map unambiguously to a
# canonical SPDX id. Versionless CC ids (e.g. bare "cc-by") are deliberately absent —
# mapping them would fabricate a version the record never stated.
_LICENSE_ID_SPDX = {
    "cc-zero": "CC0-1.0",
    "odc-pddl": "PDDL-1.0",
    "odc-by": "ODC-By-1.0",
    "odc-odbl": "ODbL-1.0",
}
_OPENDEFINITION = "opendefinition.org/licenses/"

# DCAT-US accessLevel → DataResource.access. Anything else stays None (never guessed).
_ACCESS_LEVEL = {"public": "open", "restricted public": "restricted", "non-public": "closed"}

# Every field ``_normalize`` reads, at the type it reads it as (absent or null is fine).
# Measured against 3,472 live hits (2026-10-02): none is refused.
_HIT_FIELDS = {"title": str, "description": str, "organization": dict, "dcat": dict}
_DCAT_FIELDS = {
    "title": str,
    "description": str,
    "publisher": dict,
    "keyword": list,
    "theme": list,
    "license": str,
    "accessLevel": str,
    "distribution": list,
}
_NAMED = {"name": str}
_DIST_FIELDS = {"downloadURL": str, "accessURL": str, "title": str, "format": str, "mediaType": str}


def _typed(item: object, kinds: Mapping[str, type]) -> bool:
    return isinstance(item, dict) and all(
        item.get(k) is None or isinstance(item[k], t) for k, t in kinds.items()
    )


def _is_hit(hit: object) -> bool:
    """A non-empty ``slug`` (the record's id), and every field ``_normalize`` reads at
    the type it reads it as."""
    if not (isinstance(hit, dict) and _typed(hit, _HIT_FIELDS)):
        return False
    slug, dcat = hit.get("slug"), hit.get("dcat") or {}
    return (
        isinstance(slug, str)
        and slug != ""
        and _typed(hit.get("organization") or {}, _NAMED)
        and _typed(dcat, _DCAT_FIELDS)
        and _typed(dcat.get("publisher") or {}, _NAMED)
        and all(_typed(d, _DIST_FIELDS) for d in dcat.get("distribution") or [])
    )


def _check_results(body: dict) -> None:
    """The ``results`` list every search and dataset answer carries (an empty list
    when nothing matches), each hit readable. A 200 without it is not "no hits"."""
    results = body.get("results")
    if not (isinstance(results, list) and all(_is_hit(h) for h in results)):
        raise _http.UpstreamEnvelopeError(f"no data.gov dataset list in {body!r:.200}")
    if not (body.get("after") is None or isinstance(body["after"], str)):
        raise _http.UpstreamEnvelopeError(f"no data.gov search cursor in {body!r:.200}")


def _api() -> tuple[str, str, dict[str, str]]:
    """Base URL, dataset path and headers: the keyed api.data.gov gateway when
    ``DATA_GOV_API_KEY`` is set, else the keyless catalog API."""
    key = os.environ.get("DATA_GOV_API_KEY")
    if key:
        return GATEWAY, "/dataset/", {**_ACCEPT_JSON, _KEY_HEADER: key}
    return CATALOG, "/api/dataset/", dict(_ACCEPT_JSON)


def _license(dcat: dict) -> str | None:
    """SPDX id for the DCAT ``license`` URL, or None when it names no recognizable one."""
    url = dcat.get("license")
    if not url:
        return None
    url = url.strip()
    if _OPENDEFINITION in url.lower():
        lid = PurePosixPath(urlsplit(url).path).name.lower()
        return _LICENSE_ID_SPDX.get(lid) or normalize_spdx(lid)
    return normalize_spdx(url)


def _files(dcat: dict) -> list[FileEntry]:
    """DCAT distributions with a URL — ``downloadURL`` (the file) else ``accessURL`` (an
    indirect access point). No checksum or size is published → unverified fetch."""
    out: list[FileEntry] = []
    for dist in dcat.get("distribution") or []:
        url = dist.get("downloadURL") or dist.get("accessURL")
        if not url:
            continue
        out.append(
            FileEntry(
                name=dist.get("title")
                or dist.get("format")
                or url.rpartition("/")[2]
                or "distribution",
                url=url,
                mime=dist.get("mediaType") or None,
                source="datagov",
            )
        )
    return out


def _creators(hit: dict, dcat: dict) -> list[Creator]:
    """The publishing organization is the citation author for a government dataset: the
    catalog organization (e.g. "City of New York"), else the DCAT publisher name."""
    name = ((hit.get("organization") or {}).get("name") or "").strip()
    if not name:
        name = ((dcat.get("publisher") or {}).get("name") or "").strip()
    return [Creator(name=name)] if name else []


def _label(term: object) -> object:
    """A keyword or theme as text. Some publishers give a theme as a SKOS concept,
    ``{"@type": "Concept", "prefLabel": "Tobacco Products"}`` (70 of 3,472 live hits on
    2026-10-02); its label is the theme, as the catalog's own ``theme`` field reads it."""
    return term.get("prefLabel") if isinstance(term, dict) else term


def _subjects(dcat: dict) -> list[str]:
    """DCAT keywords, then themes, de-duplicated in order."""
    out: list[str] = []
    for term in map(_label, [*(dcat.get("keyword") or []), *(dcat.get("theme") or [])]):
        if isinstance(term, str) and term and term not in out:
            out.append(term)
    return out


def _access(dcat: dict) -> str | None:
    level = dcat.get("accessLevel")
    return _ACCESS_LEVEL.get(level.strip().lower()) if level else None


def _normalize(hit: dict) -> DataResource:
    dcat = hit.get("dcat") or {}
    modified = dcat.get("modified")
    return DataResource(
        id=f"datagov:{hit['slug']}",
        source="datagov",
        kind="dataset",
        title=dcat.get("title") or hit.get("title") or "",
        creators=_creators(hit, dcat),
        year=year_from(dcat.get("issued"), modified),
        description=strip_html(dcat.get("description") or hit.get("description")),
        subjects=_subjects(dcat),
        license=_license(dcat),
        access=_access(dcat),
        # DCAT-US `modified` may be a repeating interval ("R/P1D"), not a date.
        last_updated=modified if year_from(modified) is not None else None,
        files=_files(dcat),
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """Records ``offset .. offset+size`` of the relevance-ranked hits, found by walking
    the ``after`` cursor from the start. Total is a lower bound (see module doc)."""
    want = offset + min(size, MAX_SIZE)
    base, _dataset_path, headers = _api()
    hits: list[dict] = []
    cursor: dict[str, str] = {}
    more = False
    while len(hits) < want:
        params = {"q": query, "per_page": str(min(want - len(hits), _PAGE_MAX)), **cursor}
        body = await _http.request_json(
            client,
            _GET,
            f"{base}/search",
            service="data.gov search",
            params=params,
            headers=headers,
            expect=dict,
            check=_check_results,
        )
        page = body["results"]
        hits.extend(page)
        # An empty page ends the walk even if it carries a cursor.
        more = bool(page) and bool(body.get("after"))
        if not more:
            break
        cursor = {"after": body["after"]}
    total = len(hits) + int(more)
    return total, [compact(_normalize(h)) for h in hits[offset:want]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """One dataset by catalog slug (the ``datagov:`` id; CKAN-era names still resolve)
    or by catalog dataset UUID. The lookup is an exact term match upstream."""
    ident = local_id(resource_id, "datagov")
    base, dataset_path, headers = _api()
    body = await _http.request_json(
        client,
        _GET,
        f"{base}{dataset_path}{quote(ident, safe=_SEGMENT_SAFE)}",
        service="data.gov resolve",
        headers=headers,
        not_found_returns=None,
        expect=dict,
        check=_check_results,
    )
    # The catalog answers an unknown id with 404 and an empty list.
    results = body["results"] if body is not None else []
    if not results:
        raise NotFoundError(f"data.gov has no dataset {ident!r}")
    return _normalize(results[0])
