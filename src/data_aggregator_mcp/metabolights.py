"""MetaboLights (metabolomics) file-manifest backend for OmicsDI-routed fetch.

Filenames come from the public EBI FTP directory listing itself, NOT the
MetaboLights WS ``/files`` API: the WS API returns *logical* names that don't
always match the physical file on the FTP mirror (e.g. the assay file is listed
as ``a_MTBLS1_NMR_metabolite_profiling…`` but stored as
``a_MTBLS1_metabolite_profiling…``), so WS-derived urls 404. The listing is the
same mirror we download from, so its names are guaranteed to resolve.

Top-level ISA-Tab metadata files only (raw data lives under the ``FILES/``
subdir, not descended). Returned unverified — no checksum or size.
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

FTP_DIR = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{acc}/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

# Apache autoindex hrefs. First char excludes sort links (`?C=…`) and the
# absolute parent link (`/…`); the bounded `[^"]*` is linear (no backtracking).
_HREF = re.compile(r'href="([^"?/][^"]*)"')


def _listing_files(html: str) -> list[str]:
    """File names from an Apache autoindex page — skips subdirectories (trailing
    ``/``) and de-dupes while preserving order."""
    out: list[str] = []
    for name in _HREF.findall(html):
        if name.endswith("/") or name in out:
            continue
        out.append(name)
    return out


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    base = FTP_DIR.format(acc=accession)
    resp = await _http.request_with_retry(
        client,
        "GET",
        base,
        service="MetaboLights files",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    return [
        FileEntry(
            name=name,
            url=base + name,
            size=None,
            checksum=None,
            source="metabolights",
        )
        for name in _listing_files(resp.text)
    ]
