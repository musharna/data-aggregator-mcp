import os

import httpx
import pytest

from data_aggregator_mcp import metabolights

_BODY = {
    "study": [
        {
            "file": "m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv",
            "type": "metadata_maf",
            "status": "active",
            "directory": False,
        },
        {"file": "RAW", "type": "raw", "status": "active", "directory": True},
    ],
    "latest": [],
}


@pytest.mark.asyncio
async def test_files_builds_https_urls_no_checksum_no_size():
    async def handler(request):
        assert request.url.path.endswith("/studies/MTBLS1/files")
        assert request.url.params["include_raw_data"] == "false"
        return httpx.Response(200, json=_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await metabolights.files(c, "MTBLS1")
    assert len(files) == 1  # the directory entry is dropped
    f = files[0]
    assert f.url == (
        "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/"
        "MTBLS1/m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv"
    )
    assert f.checksum is None and f.size is None
    assert f.source == "metabolights"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_metabolights_files_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await metabolights.files(c, "MTBLS1")
        assert files
        head = await c.head(files[0].url)
        assert head.status_code < 400
