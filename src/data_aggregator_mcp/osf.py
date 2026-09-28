"""OSF (osfstorage) file-manifest resolver — public nodes API, paginated, md5/sha256.

DOI 10.17605/OSF.IO/<guid> → node guid (lowercased). GET
https://api.osf.io/v2/nodes/<guid>/files/osfstorage/ → data[] (follow links.next to
paginate). A folder (kind=="folder") is listed through its
relationships.files.links.related.href, recursively, and its files keep their path
relative to the storage root ("Datafiles/raw/a.csv"): fetch plans basename
collisions on that path. Skipping folders once listed 1 of osf.io/sv3qh's 12 files.
download links 302→files.osf.io (engine follows redirects), no auth.
"""

from __future__ import annotations

from collections import deque

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

API = "https://api.osf.io/v2"
# Runaway guard on listing requests per node, across every folder and page: ~20k
# files at OSF's 10 per page. Tripping it raises; a partial manifest is never returned.
_MAX_REQUESTS = 2000


def _guid(doi: str) -> str | None:
    seg = doi.rsplit("/", 1)
    return seg[-1].lower() if len(seg) == 2 and seg[-1] else None


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    guid = _guid(doi)
    if not guid:
        return []
    out: list[FileEntry] = []
    # (first-page url, path prefix) per folder still to list; the root has prefix "".
    folders: deque[tuple[str, str]] = deque([(f"{API}/nodes/{guid}/files/osfstorage/", "")])
    requested: set[str] = set()
    while folders:
        url: str | None
        url, prefix = folders.popleft()
        while url:
            if url in requested:
                raise UpstreamUnavailableError(
                    f"OSF files for {guid}: listing revisited {url} (pagination or folder loop)"
                )
            if len(requested) >= _MAX_REQUESTS:
                raise UpstreamUnavailableError(
                    f"OSF files for {guid}: still unfinished after {_MAX_REQUESTS} listing "
                    "requests; refusing to return a partial manifest"
                )
            requested.add(url)
            body = await _http.request_json(client, "GET", url, service="OSF files")
            for item in body.get("data") or []:
                attrs = item.get("attributes") or {}
                kind = attrs.get("kind")
                name = attrs.get("name") or ""
                if kind == "folder":
                    rel = ((item.get("relationships") or {}).get("files") or {}).get("links")
                    href = ((rel or {}).get("related") or {}).get("href")
                    if not href:
                        raise UpstreamUnavailableError(
                            f"OSF files for {guid}: folder {prefix}{name!r} has no listing link"
                        )
                    folders.append((href, f"{prefix}{name}/"))
                    continue
                if kind != "file":
                    continue
                md5 = ((attrs.get("extra") or {}).get("hashes") or {}).get("md5")
                out.append(
                    FileEntry(
                        name=f"{prefix}{name}",
                        size=attrs.get("size"),
                        url=(item.get("links") or {}).get("download") or "",
                        checksum=f"md5:{md5}" if md5 else None,
                    )
                )
            url = (body.get("links") or {}).get("next")
    return out
