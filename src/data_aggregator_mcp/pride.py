"""PRIDE Archive (proteomics) file-manifest backend for OmicsDI-routed fetch.

PRIDE exposes no usable checksum (the v3 ``checksum`` field is empty), so files
are returned unverified — size-checked only. The public file URLs are ``ftp://``;
the same host serves over HTTPS, so we rewrite the scheme for httpx streaming.

The files listing is paged (0-indexed ``page``, ``pageSize`` capped at 100 upstream);
``/files/count`` gives the total, and every page up to it is read. One unpaged
request once listed 100 of PXD002179's 306 files.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

V3_FILES = "https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{acc}/files"
V3_COUNT = V3_FILES + "/count"
_PAGE_SIZE = 100  # PRIDE's ceiling: a larger pageSize still returns 100 rows
# Runaway guard: 200k files at 100 per page. A count past it raises before any
# page is read; a partial manifest is never returned.
_MAX_PAGES = 2000
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
    total = await _http.request_json(
        client,
        "GET",
        V3_COUNT.format(acc=accession),
        service="PRIDE file count",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        expect=int,
    )
    n_pages = -(-total // _PAGE_SIZE)
    if n_pages > _MAX_PAGES:
        raise UpstreamUnavailableError(
            f"PRIDE files for {accession}: {total} files need {n_pages} pages, over the "
            f"{_MAX_PAGES}-page guard; refusing to return a partial manifest"
        )
    entries: list[dict] = []
    for page in range(n_pages):
        rows = await _http.request_json(
            client,
            "GET",
            V3_FILES.format(acc=accession),
            service="PRIDE files",
            params={"pageSize": _PAGE_SIZE, "page": page},
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            expect=list,
        )
        if not rows:
            break
        entries.extend(rows)
    if len(entries) != total:
        raise UpstreamUnavailableError(
            f"PRIDE files for {accession}: paging returned {len(entries)} of {total} files"
        )
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
