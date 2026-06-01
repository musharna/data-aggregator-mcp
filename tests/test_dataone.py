import os

import httpx
import pytest

from data_aggregator_mcp import dataone
from data_aggregator_mcp.models import Creator

_SEARCH = {
    "response": {
        "numFound": 17069,
        "docs": [
            {
                "identifier": "doi:10.18739/A26336",
                "title": "Soil Probes",
                "origin": ["Jane Doe", "John Roe"],
                "datePublished": "2015-06-01T00:00:00Z",
                "dateUploaded": "2015-05-01T00:00:00Z",
                "dateModified": "2016-01-01T00:00:00Z",
                "resourceMap": ["resource_map_doi:10.18739/A26336"],
            },
            {
                "identifier": "knb-lter-jrn.20050360.9823",
                "title": "Soil Nematodes",
                "author": "John Anderson",
                "dateUploaded": "2011-12-03T00:00:00Z",
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_search_normalizes_and_prefixes_id():
    async def handler(request):
        assert request.url.path.endswith("/query/solr/")
        assert "formatType:METADATA" in request.url.params["q"]
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dataone.search(c, "soil", size=10)
    assert total == 17069
    r0 = recs[0]
    assert r0.id == "dataone:doi:10.18739/A26336" and r0.source == "dataone"
    assert r0.kind == "dataset" and r0.year == 2015
    assert r0.creators == [Creator(name="Jane Doe"), Creator(name="John Roe")]
    assert r0.last_updated == "2016-01-01T00:00:00Z"
    assert r0.files == []  # compact() drops files in search payloads
    # single-author fallback when origin absent
    assert recs[1].creators == [Creator(name="John Anderson")] and recs[1].year == 2011


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await dataone.search(c, "soil", size=5)
        assert total > 0 and recs
        assert recs[0].id.startswith("dataone:") and recs[0].source == "dataone"
