"""Pin the token-bucket arithmetic and bucket choice exactly, on a fake clock."""

from __future__ import annotations

import asyncio

import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket
from tests.test_ratelimit import FakeClock


@pytest.fixture(autouse=True)
def _reset():
    _ratelimit.reset()
    yield
    _ratelimit.reset()


class YieldingClock(FakeClock):
    """sleep() advances virtual time AND yields, so concurrent acquirers interleave."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        # A token never needs more than a few waits; a loop that sleeps (often for 0 s)
        # without ever spending is a hang, so fail it instead of spinning forever.
        assert len(self.sleeps) < 50, f"acquire is spinning: {self.sleeps[-3:]}"
        self.t += dt
        await asyncio.sleep(0)


def test_capacity_defaults_to_the_rate_but_never_below_one_token() -> None:
    assert TokenBucket(rate=3.0).capacity == 3.0
    assert TokenBucket(rate=0.5).capacity == 1.0
    assert TokenBucket(rate=1.5).capacity == 1.5
    assert TokenBucket(rate=3.0, capacity=7.0).capacity == 7.0
    assert TokenBucket(rate=3.0, capacity=0.5).capacity == 0.5


async def test_a_slow_bucket_starts_with_one_token_and_waits_a_full_period() -> None:
    clk = YieldingClock()
    b = TokenBucket(rate=0.5, now=clk.now, sleep=clk.sleep)
    await b.acquire()
    assert clk.t == 0.0
    await b.acquire()
    assert clk.sleeps == [2.0]  # one token at 0.5/s


async def test_refill_is_elapsed_time_times_rate_capped_at_capacity() -> None:
    clk = YieldingClock()
    b = TokenBucket(rate=4.0, capacity=2.0, now=clk.now, sleep=clk.sleep)
    await b.acquire()
    await b.acquire()  # empty now
    clk.t = 100.0  # a long idle refills to capacity, not to 400 tokens
    await b.acquire()
    await b.acquire()
    assert clk.sleeps == []
    await b.acquire()
    assert clk.sleeps == [0.25]  # (1 - 0 tokens) / 4 per second


async def test_a_partial_token_waits_only_for_the_missing_fraction() -> None:
    clk = YieldingClock()
    b = TokenBucket(rate=2.0, capacity=1.0, now=clk.now, sleep=clk.sleep)
    await b.acquire()
    clk.t = 0.25  # 0.5 tokens back
    await b.acquire()
    assert clk.sleeps == [pytest.approx(0.25)]  # (1 - 0.5) / 2
    assert b._tokens == 0.0  # spent exactly one, never negative


async def test_a_token_short_by_float_error_is_spent_without_sleeping() -> None:
    clk = YieldingClock()
    b = TokenBucket(rate=1.0, capacity=1.0, now=clk.now, sleep=clk.sleep)
    await b.acquire()
    b._tokens = 1.0 - 1e-12  # refill arithmetic's 0.9999999999999998
    clk.t = 0.0
    await b.acquire()
    assert clk.sleeps == []
    assert b._tokens == 0.0  # clamped, not -1e-12
    b._tokens = 1.0 - _ratelimit._EPS  # the epsilon itself is inside the tolerance
    await b.acquire()
    assert clk.sleeps == []
    b._tokens = 1.0 - 1e-6  # a real shortfall still waits
    await b.acquire()
    assert clk.sleeps == [pytest.approx(1e-6)]


async def test_concurrent_acquirers_are_served_in_arrival_order() -> None:
    """The lock serializes refill+spend, so a waiter is not overtaken by a later
    arrival that finds the token the waiter slept for."""
    clk = YieldingClock()
    b = TokenBucket(rate=3.0, capacity=3.0, now=clk.now, sleep=clk.sleep)
    done: list[int] = []

    async def one(i: int) -> None:
        await b.acquire()
        done.append(i)

    await asyncio.gather(*(one(i) for i in range(6)))
    assert done == [0, 1, 2, 3, 4, 5]
    assert clk.t == pytest.approx(1.0)


async def test_buckets_persist_per_name_with_the_rate_for_that_name(monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    await _ratelimit.acquire("NCBI esearch (pubmed)", "https://eutils.ncbi.nlm.nih.gov/x")
    ncbi = _ratelimit._BUCKETS["ncbi"]
    await _ratelimit.acquire("Zenodo search", "https://zenodo.org/api/records")
    default = _ratelimit._BUCKETS["default"]
    await _ratelimit.acquire("NCBI efetch (sra)", "https://eutils.ncbi.nlm.nih.gov/y")
    await _ratelimit.acquire("EBI OLS", "https://www.ebi.ac.uk/ols4/api/search")
    buckets = _ratelimit._BUCKETS
    assert buckets == {"ncbi": ncbi, "default": default}  # reused, not replaced
    assert (ncbi.rate, ncbi.capacity) == (3.0, 3.0)
    assert (default.rate, default.capacity) == (10.0, 10.0)
    assert ncbi._tokens == pytest.approx(1.0, abs=0.01)  # two of three spent
    assert default._tokens == pytest.approx(8.0, abs=0.01)


def test_host_is_compared_case_insensitively() -> None:
    assert _ratelimit._bucket_for("x", "https://EUTILS.NCBI.NLM.NIH.GOV/a") == "ncbi"
    assert _ratelimit._host_of("https://Zenodo.ORG/x") == "zenodo.org"
    assert _ratelimit._host_of("http://[::1") == ""  # urlsplit raises ValueError
    assert _ratelimit._host_of("/relative/path") == ""


async def test_an_unparseable_url_is_paced_by_its_label() -> None:
    await _ratelimit.acquire("NCBI esearch (geo)", "http://[::1")
    await _ratelimit.acquire("Zenodo search", "http://[::1")
    assert list(_ratelimit._BUCKETS) == ["ncbi", "default"]
    assert _ratelimit._BUCKETS["ncbi"].rate == pytest.approx(_ratelimit._ncbi_rate())
