"""Dryad file-manifest resolver — MANIFEST-ONLY.

Dryad downloads are bearer-token-gated (API) and bot-challenge-protected (web), so the
generic fetch engine cannot stream them; Dryad is intentionally excluded from the fetch
allowlist (see server._DATACITE_FETCHABLE). This resolver still populates files[] for
discovery: names, sizes, and sha-256 checksums. Two-step: dataset → latest version →
files, the latter paged via _links.next to the end.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://datadryad.org/api/v2"
_HOST = "https://datadryad.org"
_PAGE_SIZE = 100  # Dryad's per_page ceiling; its default is 20
# Runaway guard: ~100k files at 100 per page. Tripping it raises; a partial manifest
# is never returned.
_MAX_PAGES = 1000


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    enc = quote(f"doi:{doi}", safe="")
    ds = await _http.request_json(
        client, "GET", f"{BASE_URL}/datasets/{enc}", service="Dryad dataset", expect=dict
    )
    ver_href = (((ds.get("_links") or {}).get("stash:version") or {}).get("href")) or ""
    if not ver_href:
        return []
    # The files listing is paginated (20 per page by default); reading only the first
    # page listed 20 of b5mkkwhrk's 31 files. Follow _links.next to the end.
    href: str | None = f"{ver_href}/files?per_page={_PAGE_SIZE}"
    requested: set[str] = set()
    total: object = None
    embedded: list[dict] = []
    while href:
        if href in requested:
            raise UpstreamUnavailableError(f"Dryad files for {doi}: pagination revisited {href}")
        if len(requested) >= _MAX_PAGES:
            raise UpstreamUnavailableError(
                f"Dryad files for {doi}: still unfinished after {_MAX_PAGES} pages; "
                "refusing to return a partial manifest"
            )
        requested.add(href)
        fr = await _http.request_json(
            client, "GET", f"{_HOST}{href}", service="Dryad files", expect=dict
        )
        embedded.extend((fr.get("_embedded") or {}).get("stash:files") or [])
        total = fr.get("total")
        href = (((fr.get("_links") or {}).get("next") or {}).get("href")) or None
    if isinstance(total, int) and len(embedded) != total:
        raise UpstreamUnavailableError(
            f"Dryad files for {doi}: pagination ended with {len(embedded)} of {total} files"
        )
    out: list[FileEntry] = []
    for f in embedded:
        digest = f.get("digest")
        algo = (f.get("digestType") or "").replace("-", "")  # "sha-256" -> "sha256"
        self_href = (((f.get("_links") or {}).get("self") or {}).get("href")) or ""
        fid = self_href.rsplit("/", 1)[-1] if self_href else ""
        out.append(
            FileEntry(
                name=f.get("path") or "",
                size=f.get("size"),
                url=f"{_HOST}/downloads/file_stream/{fid}" if fid else "",
                checksum=f"{algo}:{digest}" if (digest and algo) else None,
            )
        )
    return out
