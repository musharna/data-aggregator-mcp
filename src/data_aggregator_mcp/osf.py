"""OSF (osfstorage) file-manifest resolver — public nodes API, paginated, md5/sha256.

DOI 10.17605/OSF.IO/<guid> → node guid (lowercased). GET
https://api.osf.io/v2/nodes/<guid>/files/osfstorage/ → data[] (follow links.next to
paginate). Folders (kind=="folder") are skipped; top-level osfstorage only (DataCite
deposits are flat). download links 302→files.osf.io (engine follows redirects), no auth.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

API = "https://api.osf.io/v2"
_MAX_FILE_PAGES = 10  # max paginated file-listing requests; ~500 files at OSF's page size


def _guid(doi: str) -> str | None:
    seg = doi.rsplit("/", 1)
    return seg[-1].lower() if len(seg) == 2 and seg[-1] else None


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    guid = _guid(doi)
    if not guid:
        return []
    url: str | None = f"{API}/nodes/{guid}/files/osfstorage/"
    out: list[FileEntry] = []
    pages_fetched = 0
    while url and pages_fetched < _MAX_FILE_PAGES:
        body = await _http.request_json(client, "GET", url, service="OSF files")
        pages_fetched += 1
        for item in body.get("data") or []:
            attrs = item.get("attributes") or {}
            if attrs.get("kind") != "file":
                continue
            md5 = ((attrs.get("extra") or {}).get("hashes") or {}).get("md5")
            out.append(
                FileEntry(
                    name=attrs.get("name") or "",
                    size=attrs.get("size"),
                    url=(item.get("links") or {}).get("download") or "",
                    checksum=f"md5:{md5}" if md5 else None,
                )
            )
        url = (body.get("links") or {}).get("next")
    return out
