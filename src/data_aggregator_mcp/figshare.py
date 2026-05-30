"""Figshare file-manifest resolver — public articles API, md5-checksummed, no auth.

The article id is the numeric run in a Figshare DOI (10.6084/m9.figshare.<id>[.vN]).
``download_url`` is an ndownloader URL that 302-redirects to signed S3 (the generic
fetch engine follows redirects).
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://api.figshare.com/v2"
_ARTICLE_ID = re.compile(r"figshare\.(\d+)")


def _article_id(doi: str) -> str | None:
    m = _ARTICLE_ID.search(doi)
    return m.group(1) if m else None


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    aid = _article_id(doi)
    if not aid:
        return []
    data = await _http.request_json(
        client, "GET", f"{BASE_URL}/articles/{aid}", service="Figshare article"
    )
    out: list[FileEntry] = []
    for f in data.get("files") or []:
        if f.get("is_link_only"):
            continue
        md5 = f.get("computed_md5") or f.get("supplied_md5")
        out.append(
            FileEntry(
                name=f.get("name") or "",
                size=f.get("size"),
                url=f.get("download_url") or "",
                checksum=f"md5:{md5}" if md5 else None,
            )
        )
    return out
