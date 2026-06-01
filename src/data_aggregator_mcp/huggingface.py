"""HuggingFace Hub *datasets* as a discovery + fetch source."""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Metrics, compact

API = "https://huggingface.co/api/datasets"
FILE_BASE = "https://huggingface.co/datasets"
PREFIXES = {"hf"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _license(tags: list[str], card: dict | None) -> str | None:
    for t in tags:
        if t.startswith("license:"):
            return t.split(":", 1)[1] or None
    return (card or {}).get("license")


def _metrics(d: dict[str, Any]) -> Metrics | None:
    """Pull HF's downloads/likes. Returns None when neither is present so the
    field stays absent rather than a zero-filled object."""
    dls, likes = d.get("downloads"), d.get("likes")
    if dls is None and likes is None:
        return None
    return Metrics(downloads=dls, likes=likes)


def _normalize(d: dict[str, Any]) -> DataResource:
    ds_id = d.get("id", "")
    tags = d.get("tags") or []
    created = d.get("createdAt") or ""
    year = int(created[:4]) if created[:4].isdigit() else None
    author = d.get("author")
    files = [
        FileEntry(name=s["rfilename"], url=f"{FILE_BASE}/{ds_id}/resolve/main/{s['rfilename']}")
        for s in (d.get("siblings") or [])
        if s.get("rfilename") and s["rfilename"] != ".gitattributes"
    ]
    return DataResource(
        id=f"hf:{ds_id}",
        source="huggingface",
        kind="dataset",
        title=ds_id,
        creators=[Creator(name=author)] if author else [],
        year=year,
        doi=None,
        license=_license(tags, d.get("cardData")),
        subjects=[t for t in tags if ":" not in t],
        access="restricted" if d.get("gated") else "open",
        metrics=_metrics(d),
        last_updated=d.get("lastModified"),
        files=files,
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    """Search HF datasets. HF paginates by Link-header cursor, not row offset, so
    this contributes to page 1 only — offset>0 returns no rows (see P4 spec)."""
    if offset:
        return 0, []
    data = await _http.request_json(
        client,
        "GET",
        API,
        service="HuggingFace search",
        params={"search": query, "limit": min(size, MAX_SIZE), "full": "true"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    items = data if isinstance(data, list) else []
    return len(items), [compact(_normalize(d)) for d in items]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    ds_id = resource_id.split(":", 1)[1] if resource_id.startswith("hf:") else resource_id
    try:
        body = await _http.request_json(
            client,
            "GET",
            f"{API}/{ds_id}",
            service="HuggingFace resolve",
            params={"full": "true"},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"HuggingFace has no dataset {ds_id!r}") from None
    return _normalize(body)
