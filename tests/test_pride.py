import os

import httpx
import pytest

from data_aggregator_mcp import pride

_FILES = [
    {
        "fileName": "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        "fileSizeBytes": 497985,
        "checksum": "",
        "publicFileLocations": [
            {
                "name": "FTP Protocol",
                "value": "ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
            },
            {
                "name": "Aspera Protocol",
                "value": "prd_ascp@fasp.ebi.ac.uk:pride/data/archive/2012/03/PXD000001/...",
            },
        ],
    },
    {"fileName": "no_public_loc.raw", "fileSizeBytes": 10, "publicFileLocations": []},
]


@pytest.mark.asyncio
async def test_files_rewrites_ftp_to_https_and_keeps_size():
    async def handler(request):
        assert request.url.path.endswith("/projects/PXD000001/files")
        return httpx.Response(200, json=_FILES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await pride.files(c, "PXD000001")
    assert len(files) == 1  # the no-public-location entry is dropped
    f = files[0]
    assert f.url == (
        "https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/"
        "PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz"
    )
    assert f.size == 497985
    assert f.checksum is None  # PRIDE exposes no usable checksum
    assert f.source == "pride"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_pride_files_https_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await pride.files(c, "PXD000001")
        assert files, "PXD000001 should list files"
        head = await c.head(files[0].url)
        assert head.status_code < 400  # rewritten HTTPS url serves
