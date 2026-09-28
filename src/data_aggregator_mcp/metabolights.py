"""MetaboLights (metabolomics) file-manifest backend for OmicsDI-routed fetch.

Filenames come from the public EBI FTP directory listing itself, NOT the
MetaboLights WS ``/files`` API: the WS API returns *logical* names that don't
always match the physical file on the FTP mirror (e.g. the assay file is listed
as ``a_MTBLS1_NMR_metabolite_profiling…`` but stored as
``a_MTBLS1_metabolite_profiling…``), so WS-derived urls 404. The listing is the
same mirror we download from, so its names are guaranteed to resolve.

Top-level ISA-Tab metadata files only (raw data lives under the ``FILES/``
subdir, not descended). Each carries the sha256 the study publishes in
``HASHES/metadata_sha256.json``; a study without that file, or a file it omits,
stays unverified. No size.
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


async def _published_sha256(client: httpx.AsyncClient, base: str) -> dict[str, str]:
    """``{filename: sha256}`` from the study's ``HASHES/metadata_sha256.json``. A 404 is
    a study that publishes none (its files stay unverified); any other failure raises —
    the hashes sit on the same mirror as the files, so hiding it would only defer it."""
    body = await _http.request_json(
        client,
        "GET",
        base + "HASHES/metadata_sha256.json",
        service="MetaboLights hashes",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={},
        expect=dict,
    )
    return {str(k): str(v) for k, v in body.items() if isinstance(v, str) and v}


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
    sha256 = await _published_sha256(client, base)
    return [
        FileEntry(
            name=name,
            url=base + name,
            size=None,
            checksum=f"sha256:{sha256[name]}" if name in sha256 else None,
            source="metabolights",
        )
        for name in _listing_files(resp.text)
    ]
