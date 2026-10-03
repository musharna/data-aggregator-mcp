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
MAX_RETRIES = 2
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}


def _is_row(row: object) -> bool:
    """A file row with every field ``files`` reads, at the type it reads it as: a
    non-empty name, an int size (or none), and a list (or none) of locations whose
    values are strings (or null)."""
    if not isinstance(row, dict):
        return False
    name, size = row.get("fileName"), row.get("fileSizeBytes")
    if not (isinstance(name, str) and name and (size is None or type(size) is int)):
        return False
    locations = row.get("publicFileLocations")
    if locations is None:
        return True
    return isinstance(locations, list) and all(
        isinstance(loc, dict) and (loc.get("value") is None or isinstance(loc.get("value"), str))
        for loc in locations
    )


def _check_rows(body: list) -> None:
    if not all(_is_row(row) for row in body):
        raise _http.UpstreamEnvelopeError(f"no PRIDE file list in {body!r:.200}")


def _check_count(body: int) -> None:
    if type(body) is not int or body < 0:  # a JSON true is an int to isinstance
        raise _http.UpstreamEnvelopeError(f"no PRIDE file count in {body!r:.200}")


def _https_url(locations: list[dict] | None) -> str | None:
    """Pick a public location and return an httpx-streamable HTTPS url, or None."""
    for loc in locations or []:
        val = loc.get("value")
        if val is None:
            continue
        if val.startswith(_FTP_HOST):
            return _HTTPS_HOST + val[len(_FTP_HOST) :]
        if val.startswith("https://"):
            return val
    return None


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    total = await _http.request_json(
        client,
        _GET,
        V3_COUNT.format(acc=accession),
        service="PRIDE file count",
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        expect=int,
        check=_check_count,
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
            _GET,
            V3_FILES.format(acc=accession),
            service="PRIDE files",
            params={"pageSize": _PAGE_SIZE, "page": page},
            headers=_ACCEPT_JSON,
            max_retries=MAX_RETRIES,
            expect=list,
            check=_check_rows,
        )
        if not rows:
            break
        entries.extend(rows)
    if len(entries) != total:
        raise UpstreamUnavailableError(
            f"PRIDE files for {accession}: paging returned {len(entries)} of {total} files"
        )
    return [f for f in map(_entry, entries) if f is not None]


def _entry(row: dict) -> FileEntry | None:
    """The file a checked row lists, or None when it has no public FTP/HTTPS location."""
    url = _https_url(row.get("publicFileLocations"))
    if not url:
        return None
    return FileEntry(
        name=row["fileName"],
        url=url,
        size=row.get("fileSizeBytes"),
        source="pride",  # no checksum: PRIDE's is empty (module docstring)
    )
