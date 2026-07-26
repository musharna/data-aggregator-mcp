import httpx
import pytest

from data_aggregator_mcp import health, sources


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
async def test_probe_sources_returns_one_result_per_target_none_dropped(httpx_mock):
    # Register one 200 per target so strict teardown is satisfied
    for _ in health._PROBE_TARGETS:
        httpx_mock.add_response(status_code=200)
    async with httpx.AsyncClient() as client:
        results = await health.probe_sources(client)
    # This compares the module's dict against itself, so it can only ever fail on
    # gather() losing or duplicating a result -- NOT on which sources are probed.
    # The coverage question is a separate, non-tautological test below.
    assert {r["name"] for r in results} == set(health._PROBE_TARGETS)
    assert len(results) == len(health._PROBE_TARGETS)


def test_probe_targets_are_real_source_names():
    """A probe target keyed on a name no source has is a silent no-op.

    ``server.py`` merges health via ``probed.get(s["name"])``, so a typo here does
    not raise -- it just yields ``health: null`` for every source, degrading
    ``check_health=true`` to a lie that looks like a working feature.
    """
    registry = {s.name for s in sources.SOURCES}
    unknown = set(health._PROBE_TARGETS) - registry
    assert not unknown, f"probe targets naming no registered source: {sorted(unknown)}"
