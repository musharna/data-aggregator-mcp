import httpx
import pytest

from data_aggregator_mcp import health


@pytest.mark.asyncio
async def test_probe_one_up(httpx_mock):
    httpx_mock.add_response(url="https://up.test/", status_code=200)
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "zenodo", "https://up.test/")
    assert r["name"] == "zenodo"
    assert r["status"] == "up"
    assert isinstance(r["latency_ms"], int)
    assert r["detail"] is None


@pytest.mark.asyncio
async def test_probe_one_down_on_5xx(httpx_mock):
    httpx_mock.add_response(url="https://down.test/", status_code=503)
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "datacite", "https://down.test/")
    assert r["status"] == "down"
    assert "503" in r["detail"]


@pytest.mark.asyncio
async def test_probe_one_down_on_transport_error_never_raises(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        r = await health._probe_one(client, "omics", "https://err.test/")
    assert r["status"] == "down"
    assert r["latency_ms"] is None
    assert r["detail"]


@pytest.mark.asyncio
async def test_probe_sources_covers_every_source(httpx_mock):
    # Register one 200 per target so strict teardown is satisfied
    for _ in health._PROBE_TARGETS:
        httpx_mock.add_response(status_code=200)
    async with httpx.AsyncClient() as client:
        results = await health.probe_sources(client)
    assert {r["name"] for r in results} == set(health._PROBE_TARGETS)
