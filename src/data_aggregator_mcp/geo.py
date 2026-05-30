"""GEO supplementary-file resolver.

GEO records (esummary db=gds) carry an ``ftplink`` base dir; downloadable
supplementary files live under ``<base>suppl/``, served as an Apache HTML
autoindex. We parse that directory listing (NOT suppl/filelist.txt, which lists
files *inside* the RAW archive). NCBI exposes no checksums here, so entries carry
``checksum=None`` (fetch is unverified; the engine's max_bytes guard still holds).
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

# Bounded (no nested quantifiers). Tolerates extra <a> attributes after href;
# NCBI's autoindex currently emits bare <a href="X"> but don't depend on it.
_HREF = re.compile(r'<a\s+href="([^"]+)"[^>]*>')


def _suppl_url(ftplink: str) -> str:
    https = ftplink.replace("ftp://", "https://", 1)
    return https.rstrip("/") + "/suppl/"


async def supplementary_files(client: httpx.AsyncClient, ftplink: str) -> list[FileEntry]:
    """Return downloadable supplementary FileEntry list for a GEO ``ftplink``.

    Empty list when ``ftplink`` is blank. Network failures propagate (fetch is a
    fail-loud path); callers that want graceful degradation wrap this.
    """
    if not ftplink:
        return []
    suppl = _suppl_url(ftplink)
    resp = await _http.request_with_retry(
        client, "GET", suppl, service="GEO suppl listing", not_found_returns=None
    )
    if resp is None:  # no suppl/ directory for this record (HTTP 404) — not an error
        return []
    out: list[FileEntry] = []
    for href in _HREF.findall(resp.text):
        # Skip parent-dir (/…), external links (http…), and Apache sort headers (?C=…).
        if href.startswith(("/", "http", "?")):
            continue
        out.append(FileEntry(name=href, url=suppl + href, size=None, checksum=None))
    return out
