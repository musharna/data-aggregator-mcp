"""Dataverse file-manifest resolver — the record's OWN Dataverse installation.

Native API: GET <base>/api/datasets/:persistentId/?persistentId=doi:<doi> →
data.latestVersion.files[].dataFile. Download = <base>/api/access/datafile/<id>
(303→signed S3; the generic fetch engine follows redirects). md5-checksummed, no auth
for RELEASED datasets.

There are ~100 Dataverse installations (Harvard, DataverseNO, DANS, DaRUS, ...), and a
DOI is only resolvable on the one that holds it. The installation is taken from the
record's landing URL (DataCite ``attributes.url``) when known; otherwise
``DATAVERSE_BASE_URL`` if the operator set one; otherwise Harvard, but ONLY for
Harvard's own DOI prefix. Anything else has no knowable installation, so no listing is
attempted (logged) rather than querying a server that cannot hold the record.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

DEFAULT_BASE_URL = "https://dataverse.harvard.edu"
# Harvard Dataverse mints under 10.7910; only those DOIs may default to Harvard.
_HARVARD_DOI_PREFIX = "10.7910/"

logger = logging.getLogger(__name__)


def _base_url(doi: str, landing_url: str | None) -> str | None:
    if landing_url:
        parts = urlsplit(landing_url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    env = os.environ.get("DATAVERSE_BASE_URL")
    if env:
        return env.rstrip("/")
    if doi.lower().startswith(_HARVARD_DOI_PREFIX):
        return DEFAULT_BASE_URL
    return None


async def files(
    client: httpx.AsyncClient, doi: str, *, landing_url: str | None = None
) -> list[FileEntry]:
    base = _base_url(doi, landing_url)
    if base is None:
        logger.warning(
            "Dataverse DOI %s: installation unknown (no landing URL, no DATAVERSE_BASE_URL, "
            "not a Harvard 10.7910 DOI); no file listing attempted",
            doi,
        )
        return []
    data = await _http.request_json(
        client,
        "GET",
        f"{base}/api/datasets/:persistentId/",
        service="Dataverse dataset",
        params={"persistentId": f"doi:{doi}"},
    )
    version = (data.get("data") or {}).get("latestVersion") or {}
    out: list[FileEntry] = []
    for f in version.get("files") or []:
        if f.get("restricted"):
            continue
        df = f.get("dataFile") or {}
        fid = df.get("id")
        if fid is None:
            continue
        md5 = df.get("md5")
        out.append(
            FileEntry(
                name=df.get("filename") or f.get("label") or "",
                size=df.get("filesize"),
                url=f"{base}/api/access/datafile/{fid}",
                checksum=f"md5:{md5}" if md5 else None,
            )
        )
    return out
