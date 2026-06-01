import os

import httpx
import pytest

from data_aggregator_mcp import omicsdi

_SEARCH = {
    "count": 740517,
    "datasets": [
        {
            "id": "MTBLS1355",
            "source": "metabolights_dataset",
            "title": "Breast Cancer Metabolomics",
            "description": "A metabolomics study.",
        },
        {
            "id": "PXD000001",
            "source": "pride",
            "title": "TMT spike-in",
            "description": "Proteomics.",
        },
        {
            "id": "GSE12345",
            "source": "omics_geo",
            "title": "Some transcriptomics",
            "description": "RNA.",
        },
    ],
}


@pytest.mark.asyncio
async def test_search_keeps_only_modality_repos():
    async def handler(request):
        assert request.url.path.endswith("/dataset/search")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await omicsdi.search(c, "cancer", size=10)
    ids = [r.id for r in recs]
    assert ids == ["omicsdi:metabolights_dataset:MTBLS1355", "omicsdi:pride:PXD000001"]
    assert total == 2  # GEO hit dropped; total is the kept count
    assert recs[0].source == "omicsdi" and recs[0].kind == "study"
    assert recs[0].files == []


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        assert await omicsdi.search(c, "x", size=10, offset=10) == (0, [])


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_modality_only():
    async with httpx.AsyncClient(timeout=60) as c:
        _total, recs = await omicsdi.search(c, "cancer", size=20)
        assert all(r.id.split(":")[1] in omicsdi._MODALITY_REPOS for r in recs)
