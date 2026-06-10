import os

import httpx
import pytest

from data_aggregator_mcp import openml
from data_aggregator_mcp.errors import NotFoundError

_LIST = {
    "data": {
        "dataset": [
            {
                "did": 61,
                "name": "iris",
                "version": 1,
                "format": "ARFF",
                "md5_checksum": "ad48",
                "file_id": 61,
                "quality": [{"name": "NumberOfInstances", "value": "150.0"}],
            },
            {
                "did": 150,
                "name": "covertype",
                "version": 3,
                "format": "ARFF",
                "md5_checksum": "bb02",
                "file_id": 150,
                "quality": [],
            },
        ]
    }
}


@pytest.mark.asyncio
async def test_search_normalizes_name_substring_hits():
    async def handler(request):
        assert "/data/list/data_name/iris" in request.url.path
        return httpx.Response(200, json=_LIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await openml.search(c, "iris", size=10)
    assert [r.id for r in recs] == ["openml:61", "openml:150"]
    assert total == 2
    assert recs[0].source == "openml" and recs[0].kind == "dataset"
    assert recs[0].title == "iris" and recs[0].files == []


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_LIST))
    ) as c:
        assert await openml.search(c, "iris", size=10, offset=10) == (0, [])


_RECORD = {
    "data_set_description": {
        "id": "61",
        "name": "iris",
        "version": "1",
        "description": "Iris plants database.",
        "format": "ARFF",
        "creator": "R.A. Fisher",
        "upload_date": "2014-04-06T23:23:39",
        "licence": "Public",
        "url": "https://openml.org/data/v1/download/61/iris.arff",
        "parquet_url": "https://data.openml.org/datasets/0000/0061/dataset_61.pq",
        "md5_checksum": "ad484452702105cbf3d30f8deaba39a9",
        "tag": ["Botany", "Ecology"],
        "paper_url": "http://example.org/paper",
    }
}


@pytest.mark.asyncio
async def test_resolve_attaches_arff_and_parquet():
    async def handler(request):
        assert request.url.path.endswith("/data/61")
        return httpx.Response(200, json=_RECORD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await openml.resolve(c, "openml:61")
    assert r.id == "openml:61" and r.title == "iris" and r.kind == "dataset"
    names = {f.name: f for f in r.files}
    assert "iris.arff" in names
    assert names["iris.arff"].checksum == "md5:ad484452702105cbf3d30f8deaba39a9"
    assert any(f.name.endswith(".pq") for f in r.files)
    assert r.license == "Public" and "Botany" in r.subjects
    assert [c.name for c in r.creators] == ["R.A. Fisher"]


@pytest.mark.asyncio
async def test_resolve_missing_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await openml.resolve(c, "openml:999999")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await openml.resolve(c, "openml:")


def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "openml" in router.available_sources()
    assert router._ADAPTERS["openml"] is openml
    assert "openml:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "openml" for s in server._SOURCES)


@pytest.mark.asyncio
async def test_router_resolve_routes_openml(monkeypatch):
    import httpx as _httpx

    from data_aggregator_mcp import router

    async def fake_resolve(client, rid):
        from data_aggregator_mcp.models import DataResource

        return DataResource(id=rid, source="openml", kind="dataset", title="x")

    monkeypatch.setattr(openml, "resolve", fake_resolve)
    async with _httpx.AsyncClient() as c:
        r = await router.resolve(c, "openml:61")
    assert r.source == "openml" and r.id == "openml:61"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await openml.search(c, "iris", size=5)
        assert recs and recs[0].source == "openml"
        full = await openml.resolve(c, recs[0].id)
        assert full.files and any(f.name.endswith((".pq", ".arff")) for f in full.files)
