"""PRIDE Archive (proteomics) file-manifest backend for OmicsDI-routed fetch.

PRIDE exposes no usable checksum (the v3 ``checksum`` field is empty), so files
are returned unverified — size-checked only. The public file URLs are ``ftp://``;
the same host serves over HTTPS, so we rewrite the scheme for httpx streaming.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

V3_FILES = "https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{acc}/files"
_FTP_HOST = "ftp://ftp.pride.ebi.ac.uk/"
_HTTPS_HOST = "https://ftp.pride.ebi.ac.uk/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _https_url(locations: list[dict] | None) -> str | None:
    """Pick a public location and return an httpx-streamable HTTPS url, or None."""
    for loc in locations or []:
        val = loc.get("value") or ""
        if val.startswith(_FTP_HOST):
            return _HTTPS_HOST + val[len(_FTP_HOST) :]
        if val.startswith("https://"):
            return val
    return None


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    body = await _http.request_json(
        client,
        "GET",
        V3_FILES.format(acc=accession),
        service="PRIDE files",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    entries = body if isinstance(body, list) else []
    out: list[FileEntry] = []
    for e in entries:
        url = _https_url(e.get("publicFileLocations"))
        if not url:
            continue
        out.append(
            FileEntry(
                name=e.get("fileName", ""),
                url=url,
                size=e.get("fileSizeBytes"),
                checksum=None,
                source="pride",
            )
        )
    return out
