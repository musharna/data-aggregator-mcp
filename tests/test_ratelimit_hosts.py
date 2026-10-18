"""Each host its own bucket; Zenodo's search and DataCite within their documented limits.

Until 2026-10-17 every host but NCBI drew from one 10 req/s bucket, so a search's ~34
requests to some 18 hosts queued behind one another, while Zenodo's search (30 a
minute per IP) and DataCite (500 per 5 minutes per IP, unidentified) went unpaced.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket, _bucket_for


@pytest.fixture(autouse=True)
def _reset():
    _ratelimit.reset()
    yield
    _ratelimit.reset()


def test_each_host_has_a_bucket_and_two_have_their_documented_limit() -> None:
    assert _bucket_for("Zenodo search", "https://zenodo.org/api/records") == "zenodo-search"
    assert _bucket_for("DataCite search", "https://api.datacite.org/dois") == "datacite"
    # Zenodo's limit is its search's: a record read is the host's own bucket.
    assert _bucket_for("Zenodo record", "https://zenodo.org/api/records/1254563") == "zenodo.org"
    assert _bucket_for("GBIF", "https://api.gbif.org/v1/dataset/search") == "api.gbif.org"
    assert _bucket_for("x", "https://datacite.org.example/dois") == "datacite.org.example"


async def test_one_host_spending_its_bucket_leaves_another_hosts_full() -> None:
    for _ in range(10):
        await _ratelimit.acquire("MassIVE search", "https://massive.ucsd.edu/q")
    await _ratelimit.acquire("GBIF", "https://api.gbif.org/v1/dataset/search")
    massive = _ratelimit._BUCKETS["massive.ucsd.edu"]
    gbif = _ratelimit._BUCKETS["api.gbif.org"]
    assert massive is not gbif
    assert massive._tokens == pytest.approx(0.0, abs=0.5)  # positive control: it was spent
    assert gbif._tokens == pytest.approx(9.0, abs=0.01)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


def _as_built(url: str, clk: _Clock) -> TokenBucket:
    """The bucket ``acquire`` builds for ``url``, full again and on ``clk``."""
    asyncio.run(_ratelimit.acquire("x", url))
    bucket = _ratelimit._BUCKETS[_bucket_for("x", url)]
    assert isinstance(bucket, TokenBucket)
    bucket._now, bucket._sleep = clk.now, clk.sleep
    bucket._updated, bucket._tokens = clk.t, bucket.capacity
    return bucket


@pytest.mark.parametrize(
    ("url", "documented", "window"),
    [
        ("https://zenodo.org/api/records", 30, 60.0),
        ("https://api.datacite.org/dois", 500, 300.0),
    ],
)
def test_no_window_holds_more_than_the_documented_limit_and_a_search_goes_at_once(
    url, documented, window
) -> None:
    clk = _Clock()
    bucket = _as_built(url, clk)
    requests = round(bucket.capacity + bucket.rate * window)
    assert requests <= documented
    burst = int(bucket.capacity)
    rng = random.Random(20261017)
    granted: list[float] = []

    async def run() -> None:
        for _ in range(3 * requests):
            clk.t += rng.expovariate(2 * requests / window)  # twice the rate allowed
            await bucket.acquire()
            granted.append(clk.t)

    asyncio.run(run())
    most = max(sum(t - window < u <= t for u in granted) for t in granted)
    assert most <= requests
    assert most >= requests - 1  # positive control: the load did reach the limit
    # A search's requests (Zenodo 3, DataCite 4 in the round-3 replay) go at once to a
    # rested bucket.
    assert burst >= 4
    _ratelimit.reset()
    rested = _as_built(url, clk)
    before = clk.t

    async def search() -> None:
        for _ in range(burst):
            await rested.acquire()

    asyncio.run(search())
    assert clk.t == before
