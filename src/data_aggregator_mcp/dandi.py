"""DANDI Archive — neurophysiology dandisets (NWB), native adapter.

Search/resolve over the DANDI REST API; resolve attaches the published-version
metadata (DOI is minted on PUBLISHED versions only — a draft's doi is null) plus
a file manifest of the first ASSET_PAGE assets, each a download URL that
302-redirects to S3 (the generic fetch engine follows redirects). A dandiset can
be many GB / thousands of assets, so the manifest is capped and the cap is
documented — this is a manifest, not a guarantee of a bulk pull. kind="dataset".
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    Link,
    compact,
    local_id,
    year_from,
)

API = "https://api.dandiarchive.org/api"
_LANDING = "https://dandiarchive.org/dandiset/{id}"
_DOWNLOAD = "https://api.dandiarchive.org/api/assets/{asset_id}/download/"
PREFIXES = {"dandi"}
# A dandiset id is digits (000004); it goes into the URL path.
_IDENT_RE = re.compile(r"[0-9]+")
DEFAULT_SIZE = 10
MAX_SIZE = 50
ASSET_PAGE = 100  # manifest cap; large dandisets are truncated (documented)
MAX_RETRIES = 2
_ACCEPT_JSON = {"Accept": "application/json"}


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    service: str,
    expect: type | tuple[type, ...],
    **kwargs: Any,
) -> Any:
    """GET ``url`` as JSON with this source's retry budget and ``_http``'s timeout."""
    # httpx upper-cases the method, so "get" would send the same request.
    method = "GET"  # pragma: no mutate
    return await _http.request_json(
        client,
        method,
        url,
        service=service,
        expect=expect,
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        **kwargs,
    )


def _active_version(d: dict) -> dict:
    """The version to describe: prefer the published one, else the draft."""
    return d.get("most_recent_published_version") or d.get("draft_version") or {}


def _normalize_listing(d: dict) -> DataResource:
    ident = d.get("identifier") or ""
    ver = _active_version(d)
    return DataResource(
        id=f"dandi:{ident}",
        source="dandi",
        kind="dataset",
        title=ver.get("name") or ident,
        year=year_from(d.get("created")),
        last_updated=d.get("modified") or None,
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
    body = await _get_json(
        client,
        f"{API}/dandisets/",
        service="DANDI search",
        params={"search": query, "page_size": capped, "page": page},
        # No not_found_returns: a 404 on the LIST endpoint means it moved (an outage);
        # a search that matches nothing is a 200 with count 0.
        expect=dict,
    )
    results = body.get("results") or []
    total = body.get("count", len(results))
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
    body = await _get_json(
        client,
        f"{API}/dandisets/{ident}/versions/{version}/assets/",
        service="DANDI assets",
        # metadata=true returns each asset's metadata, whose digest carries the sha256
        # DANDI computed — the bare list has none, and fetch could not verify.
        params={"page_size": ASSET_PAGE, "metadata": "true"},
        # No not_found_returns: this version was just read off the dandiset itself,
        # so a 404 here is a failure, not "the dandiset has no assets".
        expect=dict,
    )
    out: list[FileEntry] = []
    for a in body.get("results") or []:
        aid = a.get("asset_id")
        if not aid:
            continue
        digest = ((a.get("metadata") or {}).get("digest") or {}).get("dandi:sha2-256")
        out.append(
            FileEntry(
                name=a.get("path") or aid,
                size=a.get("size"),
                url=_DOWNLOAD.format(asset_id=aid),
                # Not yet computed for a fresh upload: that asset stays unverified.
                checksum=f"sha256:{digest}" if digest else None,
                source="dandi",
            )
        )
    return out


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ident = local_id(resource_id, "dandi", strip=True)
    if not _IDENT_RE.fullmatch(ident):
        raise NotFoundError(f"malformed DANDI id {resource_id!r}")
    detail = await _get_json(
        client,
        f"{API}/dandisets/{ident}/",
        service="DANDI resolve",
        # A 404 is "no such dandiset". A 200 that is not an object is a broken answer:
        # `null` or `[]` must not read as that 404.
        not_found_returns=None,
        expect=dict,
    )
    if not detail or not detail.get("identifier"):
        raise NotFoundError(f"DANDI has no dandiset {ident}")
    ver = _active_version(detail)
    version = ver.get("version") or "draft"
    info = await _get_json(
        client,
        f"{API}/dandisets/{ident}/versions/{version}/info/",
        service="DANDI version info",
        # A 404 means this version has no info page: fall back to the detail's fields.
        # Anything but an object on a 200 is a broken answer, not missing metadata.
        not_found_returns={},
        expect=dict,
    )
    meta = info.get("metadata") or {}
    return DataResource(
        id=f"dandi:{ident}",
        source="dandi",
        kind="dataset",
        title=meta.get("name") or ver.get("name") or ident,
        creators=_creators(meta.get("contributor") or []),
        year=year_from(detail.get("created")),
        doi=meta.get("doi"),
        license=_license(meta.get("license")),
        access="open",
        last_updated=detail.get("modified") or None,
        files=await _asset_manifest(client, ident, version),
        links=[Link(rel="landing_page", target_id=meta.get("url") or _LANDING.format(id=ident))],
    )
