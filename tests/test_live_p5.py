import os
import time

import httpx
import pytest

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 for live probes")


@pytest.mark.asyncio
async def test_live_health_probe_shape():
    from data_aggregator_mcp import health

    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await health.probe_sources(client)
    assert {r["name"] for r in results} == set(health._PROBE_TARGETS)
    for r in results:
        assert r["status"] in {"up", "down"}
        if r["status"] == "up":
            assert isinstance(r["latency_ms"], int)


@pytest.mark.asyncio
async def test_live_ncbi_rate_pacing():
    """6 real NCBI esearch calls on a 3/s bucket must span >= ~1s (real CLI path,
    not a mock) — the real-execution check that the limiter actually paces."""
    from data_aggregator_mcp import _eutils, _ratelimit

    _ratelimit.reset()
    async with httpx.AsyncClient() as client:
        start = time.monotonic()
        for _ in range(6):
            await _eutils.esearch(client, "pubmed", "cancer", retmax=1)
        elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # ~ (6 - capacity 3) / 3 s of forced pacing
