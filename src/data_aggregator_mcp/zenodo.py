"""Zenodo archives adapter — async wrapper around zenodo.org REST API.

Discovery: GET /api/records?q=...  |  Resolve: GET /api/records/{id}
Zenodo records carry a checksummed file manifest, so this single source
exercises the full search → resolve → fetch loop.
"""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    FundingRef,
    _orcid,
    compact,
    normalize_access,
)

BASE_URL = "https://zenodo.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_SIZE = 10
MAX_SIZE = 50

# Zenodo resource_type.type → DataResource.kind
_KIND_MAP = {
    "dataset": "dataset",
    "publication": "publication",
    "software": "software",
}


def _normalize(record: dict[str, Any]) -> DataResource:
    meta = record.get("metadata", {}) or {}
    rtype = (meta.get("resource_type") or {}).get("type", "dataset")
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
    return DataResource(
        id=f"zenodo:{record.get('id')}",
        source="zenodo",
        kind=_KIND_MAP.get(rtype, "dataset"),
        title=meta.get("title", ""),
        creators=[
            Creator(name=c.get("name", ""), orcid=_orcid(c.get("orcid")))
            for c in meta.get("creators", []) or []
        ],
        funding=[
            FundingRef(
                funder=(g.get("funder") or {}).get("name"), award=g.get("code") or g.get("title")
            )
            for g in (meta.get("grants") or [])
            if (g.get("funder") or {}).get("name")
        ],
        year=year,
        description=meta.get("description"),
        doi=record.get("doi"),
        subjects=list(meta.get("keywords", []) or []),
        license=(meta.get("license") or {}).get("id"),
        access=normalize_access(meta.get("access_right")),
        files=files,
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Search Zenodo records. Returns (total_hits, COMPACT resources).

    ``offset`` selects the window [offset, offset+size): request page
    ``offset // size + 1`` at page-size ``size`` and drop the first
    ``offset % size`` records (page-boundary slice; see pagination spec).
    """
    capped = min(size, MAX_SIZE)
    params = {"q": query, "size": str(capped)}
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
    return total, [compact(_normalize(r)) for r in sliced]


async def resolve(client: httpx.AsyncClient, record_id: str) -> DataResource:
    """Resolve a Zenodo record by id (``zenodo:123`` or bare ``123``)."""
    rid = record_id.split(":", 1)[1] if record_id.startswith("zenodo:") else record_id
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
    return _normalize(record)
