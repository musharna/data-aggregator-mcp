"""MetaboLights (metabolomics) file-manifest backend for OmicsDI-routed fetch.

The MetaboLights files API exposes neither checksum nor size, so files are
returned fully unverified. Bytes are served from the EBI FTP HTTPS mirror.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

WS_FILES = "https://www.ebi.ac.uk/metabolights/ws/studies/{acc}/files"
FTP_BASE = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{acc}/{file}"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    body = await _http.request_json(
        client,
        "GET",
        WS_FILES.format(acc=accession),
        service="MetaboLights files",
        params={"include_raw_data": "false"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    study = (body or {}).get("study") or []
    out: list[FileEntry] = []
    for e in study:
        fname = e.get("file")
        if not fname or e.get("directory") is True:
            continue
        out.append(
            FileEntry(
                name=fname,
                url=FTP_BASE.format(acc=accession, file=fname),
                size=None,
                checksum=None,
                source="metabolights",
            )
        )
    return out
