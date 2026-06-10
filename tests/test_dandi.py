import os

import httpx
import pytest

from data_aggregator_mcp import dandi
from data_aggregator_mcp.errors import NotFoundError

_SEARCH = {
    "count": 291,
    "results": [
        {
            "identifier": "000003",
            "created": "2019-09-15T01:02:03Z",
            "modified": "2021-01-02T00:00:00Z",
            "embargo_status": "OPEN",
            "most_recent_published_version": {
                "name": "Hippocampus ephys",
                "version": "0.220126.1852",
                "asset_count": 87,
                "size": 6197474020,
            },
            "draft_version": {"name": "Hippocampus ephys", "version": "draft"},
        },
        {
            "identifier": "000999",
            "created": "2022-05-01T00:00:00Z",
            "modified": "2022-05-02T00:00:00Z",
            "embargo_status": "OPEN",
            "most_recent_published_version": None,
            "draft_version": {"name": "Unpublished set", "version": "draft"},
        },
    ],
}


@pytest.mark.asyncio
async def test_search_normalizes_dandisets():
    async def handler(request):
        assert request.url.path.endswith("/dandisets/")
        assert request.url.params.get("search") == "hippocampus"
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dandi.search(c, "hippocampus", size=2)
    assert total == 291
    assert [r.id for r in recs] == ["dandi:000003", "dandi:000999"]
    assert recs[0].title == "Hippocampus ephys" and recs[0].year == 2019
    assert recs[0].source == "dandi" and recs[0].kind == "dataset"
    assert recs[1].title == "Unpublished set"


@pytest.mark.asyncio
async def test_search_paginates_1_indexed():
    captured = {}

    async def handler(request):
        captured["page"] = request.url.params.get("page")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await dandi.search(c, "x", size=10, offset=20)
    assert captured["page"] == "3"


@pytest.mark.asyncio
async def test_search_page_and_page_size_agree_past_max_size():
    # size > MAX_SIZE: page must derive from the CAPPED page_size, not raw size,
    # and the offset%capped remainder is sliced off the returned page.
    captured = {}

    async def handler(request):
        captured["page"] = request.url.params.get("page")
        captured["page_size"] = request.url.params.get("page_size")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dandi.search(c, "x", size=100, offset=100)
    # capped = 50 → page = 100//50 + 1 = 3, page_size = 50, slice offset%50 = 0
    assert captured["page_size"] == "50"
    assert captured["page"] == "3"
    assert [r.id for r in recs] == ["dandi:000003", "dandi:000999"]


@pytest.mark.asyncio
async def test_search_slices_offset_remainder():
    # offset not page-aligned: drop the first offset%capped records of the page.
    async def handler(request):
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        _, recs = await dandi.search(c, "x", size=2, offset=1)
    # capped=2, offset%2=1 → drop first record, keep the second
    assert [r.id for r in recs] == ["dandi:000999"]


_DETAIL = {
    "identifier": "000004",
    "created": "2020-03-16T21:48:04Z",
    "modified": "2021-08-12T00:00:00Z",
    "most_recent_published_version": {
        "name": "Human single-neuron memory",
        "version": "0.220126.1852",
        "asset_count": 2,
        "size": 1000,
    },
    "draft_version": {"name": "Human single-neuron memory", "version": "draft"},
}
_INFO = {
    "metadata": {
        "name": "Human single-neuron memory",
        "doi": "10.48324/dandi.000004/0.220126.1852",
        "license": ["spdx:CC-BY-4.0"],
        "contributor": [
            {"name": "Chandravadia, Nand", "roleName": ["dcite:Author", "dcite:ContactPerson"]},
            {"name": "Rutishauser, Ueli", "roleName": ["dcite:Author"]},
            {"name": "NIH", "roleName": ["dcite:Funder"]},
        ],
        "url": "https://dandiarchive.org/dandiset/000004/0.220126.1852",
    }
}
_ASSETS = {
    "count": 2,
    "results": [
        {"asset_id": "aaa-111", "path": "sub-01/sub-01_ecephys.nwb", "size": 73156888},
        {"asset_id": "bbb-222", "path": "sub-02/sub-02_ecephys.nwb", "size": 55262212},
    ],
}


def _resolve_router(request):
    p = request.url.path
    if p.endswith("/info/"):
        return httpx.Response(200, json=_INFO)
    if p.endswith("/assets/"):
        return httpx.Response(200, json=_ASSETS)
    if p.endswith("/dandisets/000004/"):
        return httpx.Response(200, json=_DETAIL)
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_resolve_attaches_metadata_and_asset_manifest():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_resolve_router)) as c:
        r = await dandi.resolve(c, "dandi:000004")
    assert r.id == "dandi:000004" and r.kind == "dataset"
    assert r.doi == "10.48324/dandi.000004/0.220126.1852"
    assert r.license == "spdx:CC-BY-4.0"
    assert [c.name for c in r.creators] == ["Chandravadia, Nand", "Rutishauser, Ueli"]
    assert [f.name for f in r.files] == ["sub-01/sub-01_ecephys.nwb", "sub-02/sub-02_ecephys.nwb"]
    assert r.files[0].url == "https://api.dandiarchive.org/api/assets/aaa-111/download/"
    assert r.files[0].source == "dandi"


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await dandi.resolve(c, "dandi:999999")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await dandi.resolve(c, "dandi:")


def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "dandi" in router.available_sources()
    assert router._ADAPTERS["dandi"] is dandi
    assert "dandi:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "dandi" for s in server._SOURCES)


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await dandi.search(c, "mouse", size=3)
        assert total > 0 and recs and recs[0].id.startswith("dandi:")
        full = await dandi.resolve(c, recs[0].id)
        assert full.kind == "dataset" and full.files
