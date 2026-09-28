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
from urllib.parse import quote

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
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

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


def _route(kind: str, ident: str = "") -> tuple[str, dict[str, str]]:
    """URL + headers for ``kind`` ("search" | "dataset"): the keyed api.data.gov gateway
    when ``DATA_GOV_API_KEY`` is set, else the keyless catalog API."""
    key = os.environ.get("DATA_GOV_API_KEY")
    headers = {"Accept": "application/json"}
    if key:
        headers["X-Api-Key"] = key
        base, dataset_path = GATEWAY, "/dataset/"
    else:
        base, dataset_path = CATALOG, "/api/dataset/"
    if kind == "search":
        return f"{base}/search", headers
    return f"{base}{dataset_path}{quote(ident, safe='')}", headers


def _license(dcat: dict) -> str | None:
    """SPDX id for the DCAT ``license`` URL, or None when it names no recognizable one."""
    url = (dcat.get("license") or "").strip()
    if _OPENDEFINITION in url.lower():
        lid = url.lower().rstrip("/").rsplit("/", 1)[-1]
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
                or url.rsplit("/", 1)[-1]
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


def _subjects(dcat: dict) -> list[str]:
    """DCAT keywords, then themes, de-duplicated in order."""
    out: list[str] = []
    for term in [*(dcat.get("keyword") or []), *(dcat.get("theme") or [])]:
        if isinstance(term, str) and term and term not in out:
            out.append(term)
    return out


def _normalize(hit: dict) -> DataResource:
    dcat = hit.get("dcat") or {}
    modified = dcat.get("modified")
    access_level = (dcat.get("accessLevel") or "").strip().lower()
    return DataResource(
        id=f"datagov:{hit.get('slug') or ''}",
        source="datagov",
        kind="dataset",
        title=dcat.get("title") or hit.get("title") or "",
        creators=_creators(hit, dcat),
        year=year_from(dcat.get("issued"), modified),
        description=strip_html(dcat.get("description") or hit.get("description")),
        subjects=_subjects(dcat),
        license=_license(dcat),
        access=_ACCESS_LEVEL.get(access_level),
        # DCAT-US `modified` may be a repeating interval ("R/P1D"), not a date.
        last_updated=modified if year_from(modified) is not None else None,
        links=[],
        files=_files(dcat),
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """Records ``offset .. offset+size`` of the relevance-ranked hits, found by walking
    the ``after`` cursor from the start. Total is a lower bound (see module doc)."""
    want = offset + min(size, MAX_SIZE)
    url, headers = _route("search")
    hits: list[dict] = []
    after: str | None = None
    while len(hits) < want:
        params = {"q": query, "per_page": str(min(want - len(hits), _PAGE_MAX))}
        if after:
            params["after"] = after
        body = await _http.request_json(
            client,
            "GET",
            url,
            service="data.gov search",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        page = body.get("results") or []
        hits.extend(page)
        after = body.get("after") if page else None
        if not after:
            break
    total = len(hits) + (1 if after else 0)
    return total, [compact(_normalize(h)) for h in hits[offset:want]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """One dataset by catalog slug (the ``datagov:`` id; CKAN-era names still resolve)
    or by catalog dataset UUID. The lookup is an exact term match upstream."""
    ident = local_id(resource_id, "datagov")
    url, headers = _route("dataset", ident)
    body = await _http.request_json(
        client,
        "GET",
        url,
        service="data.gov resolve",
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    results = (body or {}).get("results") or []
    if not results:
        raise NotFoundError(f"data.gov has no dataset {ident!r}")
    return _normalize(results[0])
