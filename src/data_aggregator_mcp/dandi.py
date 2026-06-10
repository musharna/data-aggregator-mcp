"""DANDI Archive — neurophysiology dandisets (NWB), native adapter.

Search/resolve over the DANDI REST API; resolve attaches the published-version
metadata (DOI is minted on PUBLISHED versions only — a draft's doi is null) plus
a file manifest of the first ASSET_PAGE assets, each a download URL that
302-redirects to S3 (the generic fetch engine follows redirects). A dandiset can
be many GB / thousands of assets, so the manifest is capped and the cap is
documented — this is a manifest, not a guarantee of a bulk pull. kind="dataset".
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    Link,
    compact,
    normalize_access,
)

API = "https://api.dandiarchive.org/api"
_LANDING = "https://dandiarchive.org/dandiset/{id}"
_DOWNLOAD = "https://api.dandiarchive.org/api/assets/{asset_id}/download/"
PREFIXES = {"dandi"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
ASSET_PAGE = 100  # manifest cap; large dandisets are truncated (documented)
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _active_version(d: dict) -> dict:
    """The version to describe: prefer the published one, else the draft."""
    return d.get("most_recent_published_version") or d.get("draft_version") or {}


def _normalize_listing(d: dict) -> DataResource:
    ident = d.get("identifier") or ""
    ver = _active_version(d)
    created = d.get("created") or ""
    year = int(created[:4]) if created[:4].isdigit() else None
    return DataResource(
        id=f"dandi:{ident}",
        source="dandi",
        kind="dataset",
        title=ver.get("name") or ident,
        year=year,
        last_updated=d.get("modified") or None,
        files=[],
        links=[Link(rel="landing_page", target_id=_LANDING.format(id=ident))],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    # DANDI pages 1-indexed at a fixed page_size; request page ``offset // capped + 1``
    # at page-size ``capped`` and drop the first ``offset % capped`` records so an
    # arbitrary offset still maps onto the window [offset, offset+size). page and
    # page_size must agree on ``capped`` (not raw size) or paging past MAX_SIZE skews.
    capped = min(size, MAX_SIZE)
    page = offset // capped + 1 if capped else 1
    body = await _http.request_json(
        client,
        "GET",
        f"{API}/dandisets/",
        service="DANDI search",
        params={"search": query, "page_size": capped, "page": page},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={"count": 0, "results": []},
    )
    results = (body or {}).get("results") or []
    total = (body or {}).get("count", len(results))
    sliced = results[offset % capped :] if capped else results
    return total, [compact(_normalize_listing(d)) for d in sliced]


# dcite contributor roles we treat as authorship (others: Funder, Sponsor, ...).
_AUTHOR_ROLES = {"dcite:Author", "dcite:Creator"}


def _creators(contributors: list[dict]) -> list[Creator]:
    out: list[Creator] = []
    for c in contributors:
        roles = c.get("roleName") or []
        name = c.get("name")
        if name and any(r in _AUTHOR_ROLES for r in roles):
            out.append(Creator(name=name))
    return out


def _license(raw: list | str | None) -> str | None:
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return raw if isinstance(raw, str) else None


async def _asset_manifest(client: httpx.AsyncClient, ident: str, version: str) -> list[FileEntry]:
    body = await _http.request_json(
        client,
        "GET",
        f"{API}/dandisets/{ident}/versions/{version}/assets/",
        service="DANDI assets",
        params={"page_size": ASSET_PAGE},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={"results": []},
    )
    out: list[FileEntry] = []
    for a in (body or {}).get("results") or []:
        aid = a.get("asset_id")
        if not aid:
            continue
        out.append(
            FileEntry(
                name=a.get("path") or aid,
                size=a.get("size"),
                url=_DOWNLOAD.format(asset_id=aid),
                source="dandi",
            )
        )
    return out


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ident = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not ident:
        raise NotFoundError(f"malformed DANDI id {resource_id!r}")
    detail = await _http.request_json(
        client,
        "GET",
        f"{API}/dandisets/{ident}/",
        service="DANDI resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if not detail or not detail.get("identifier"):
        raise NotFoundError(f"DANDI has no dandiset {ident}")
    ver = _active_version(detail)
    version = ver.get("version") or "draft"
    info = await _http.request_json(
        client,
        "GET",
        f"{API}/dandisets/{ident}/versions/{version}/info/",
        service="DANDI version info",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={},
    )
    meta = (info or {}).get("metadata") or {}
    created = detail.get("created") or ""
    return DataResource(
        id=f"dandi:{ident}",
        source="dandi",
        kind="dataset",
        title=meta.get("name") or ver.get("name") or ident,
        creators=_creators(meta.get("contributor") or []),
        year=int(created[:4]) if created[:4].isdigit() else None,
        doi=meta.get("doi"),
        license=_license(meta.get("license")),
        access=normalize_access("open"),
        last_updated=detail.get("modified") or None,
        files=await _asset_manifest(client, ident, version),
        links=[Link(rel="landing_page", target_id=meta.get("url") or _LANDING.format(id=ident))],
    )
