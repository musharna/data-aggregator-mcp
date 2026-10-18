"""Pin the token-bucket arithmetic and bucket choice exactly, on a fake clock."""

from __future__ import annotations

import asyncio
import logging

import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket
from tests.test_ratelimit import FakeClock, InjectedSleepOnly


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


async def test_buckets_persist_per_name_with_the_rate_for_that_name(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setenv(_ratelimit.STATE_DIR_ENV, str(tmp_path))
    await _ratelimit.acquire("NCBI esearch (pubmed)", "https://eutils.ncbi.nlm.nih.gov/x")
    ncbi = _ratelimit._BUCKETS["ncbi"]
    await _ratelimit.acquire("Zenodo search", "https://zenodo.org/api/records")
    search = _ratelimit._BUCKETS["zenodo-search"]
    await _ratelimit.acquire("EBI OLS", "https://www.ebi.ac.uk/ols4/api/search")
    ebi = _ratelimit._BUCKETS["www.ebi.ac.uk"]
    await _ratelimit.acquire("EBI OLS", "https://www.ebi.ac.uk/ols4/api/search")
    await _ratelimit.acquire("NCBI efetch (sra)", "https://eutils.ncbi.nlm.nih.gov/y")
    buckets = _ratelimit._BUCKETS
    assert buckets == {"ncbi": ncbi, "zenodo-search": search, "www.ebi.ac.uk": ebi}  # reused
    # NCBI: the machine-wide schedule in the state directory, 2/s with no burst.
    assert isinstance(ncbi, _ratelimit.SharedSchedule)
    assert ncbi.path == tmp_path / "ncbi.schedule"
    assert ncbi.rate == pytest.approx(2.0)
    # Zenodo's search: its documented 30 a minute, at 90%: a burst of 4, then 23 a minute.
    assert isinstance(search, TokenBucket)
    assert (search.capacity, search.rate) == (4, pytest.approx(23 / 60))
    assert (ebi.rate, ebi.capacity) == (10.0, 10.0)
    assert ebi._tokens == pytest.approx(8.0, abs=0.01)  # its own two, no one else's


def test_host_is_compared_case_insensitively() -> None:
    assert _ratelimit._bucket_for("x", "https://EUTILS.NCBI.NLM.NIH.GOV/a") == "ncbi"
    assert _ratelimit._host_path("https://Zenodo.ORG/x") == ("zenodo.org", "/x")
    assert _ratelimit._host_path("http://[::1") == ("", "")  # urlsplit raises ValueError
    assert _ratelimit._host_path("/relative/path") == ("", "/relative/path")
    assert _ratelimit._bucket_for("x", "https://ZENODO.org/api/records/") == "zenodo-search"


async def test_an_unparseable_url_is_paced_by_its_label() -> None:
    await _ratelimit.acquire("NCBI esearch (geo)", "http://[::1")
    await _ratelimit.acquire("Zenodo search", "http://[::1")
    assert list(_ratelimit._BUCKETS) == ["ncbi", "default"]
    assert _ratelimit._BUCKETS["ncbi"].rate == pytest.approx(_ratelimit._ncbi_rate())


async def test_without_flock_the_schedule_paces_this_process_and_touches_no_file(
    tmp_path, caplog, monkeypatch
) -> None:
    """Windows has no ``fcntl``: the schedule paces this process alone from the start,
    with no burst, through the sleep it was given, and never opens the lock file."""
    monkeypatch.setattr(_ratelimit, "fcntl", None)
    monkeypatch.setattr(_ratelimit, "TokenBucket", InjectedSleepOnly)
    clk = FakeClock()
    path = tmp_path / "state" / "ncbi.schedule"
    schedule = _ratelimit.SharedSchedule(path, 2.0, now=clk.now, sleep=clk.sleep)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp._ratelimit"):
        for _ in range(3):
            await schedule.acquire()
    assert clk.t == pytest.approx(1.0)  # positive control: one free slot, then 0.5 s apart
    assert clk.naps == 2
    assert not path.parent.exists()
    assert caplog.records == []  # nothing failed, so nothing to say


async def test_a_missing_state_directory_is_created_with_its_parents(tmp_path, caplog) -> None:
    """A fresh machine has no ``~/.cache`` either: the claim creates the whole path, so the
    schedule is shared from the first request instead of falling back per process."""
    clk = FakeClock()
    clk.t = 1000.0
    path = tmp_path / "cache" / "data-aggregator-mcp" / "ncbi.schedule"
    schedule = _ratelimit.SharedSchedule(path, 4.0, now=clk.now, sleep=clk.sleep)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp._ratelimit"):
        await schedule.acquire()
        await schedule.acquire()
    assert caplog.records == []
    assert schedule._local is None  # still the shared schedule
    assert float(path.read_bytes()) == 1000.5  # two slots claimed from the file
    assert clk.t == 1000.25  # the second waited one period


async def test_a_slot_exactly_the_horizon_ahead_is_honoured(tmp_path) -> None:
    clk = FakeClock()
    clk.t = 1000.0
    path = tmp_path / "s"
    path.write_bytes(repr(1000.0 + _ratelimit._MAX_AHEAD).encode())
    await _ratelimit.SharedSchedule(path, 4.0, now=clk.now, sleep=clk.sleep).acquire()
    assert clk.t == 1000.0 + _ratelimit._MAX_AHEAD  # a full queue, waited out
    # Control: a quarter second further ahead is a jumped clock, replaced by now.
    path.write_bytes(repr(clk.t + _ratelimit._MAX_AHEAD + 0.25).encode())
    before = clk.t
    await _ratelimit.SharedSchedule(path, 4.0, now=clk.now, sleep=clk.sleep).acquire()
    assert clk.t == before
    assert float(path.read_bytes()) == before + 0.25
