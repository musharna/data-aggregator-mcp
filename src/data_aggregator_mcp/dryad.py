"""Dryad file-manifest resolver — MANIFEST-ONLY.

Dryad downloads are bearer-token-gated (API) and bot-challenge-protected (web), so the
generic fetch engine cannot stream them; Dryad is intentionally excluded from the fetch
allowlist (see server._DATACITE_FETCHABLE). This resolver still populates files[] for
discovery: names, sizes, and sha-256 checksums. Two-step: dataset → latest version → files.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://datadryad.org/api/v2"
_HOST = "https://datadryad.org"


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    enc = quote(f"doi:{doi}", safe="")
    ds = await _http.request_json(
        client, "GET", f"{BASE_URL}/datasets/{enc}", service="Dryad dataset"
    )
    ver_href = (((ds.get("_links") or {}).get("stash:version") or {}).get("href")) or ""
    if not ver_href:
        return []
    fr = await _http.request_json(client, "GET", f"{_HOST}{ver_href}/files", service="Dryad files")
    embedded = (fr.get("_embedded") or {}).get("stash:files") or []
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
