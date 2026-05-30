"""Dataverse file-manifest resolver — Harvard by default, DATAVERSE_BASE_URL override.

Native API: GET <base>/api/datasets/:persistentId/?persistentId=doi:<doi> →
data.latestVersion.files[].dataFile. Download = <base>/api/access/datafile/<id>
(303→signed S3; the generic fetch engine follows redirects). md5-checksummed, no auth
for RELEASED datasets. Multi-instance auto-discovery is out of scope (Harvard default).
"""

from __future__ import annotations

import os

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

DEFAULT_BASE_URL = "https://dataverse.harvard.edu"


def _base_url() -> str:
    return os.environ.get("DATAVERSE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    base = _base_url()
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
