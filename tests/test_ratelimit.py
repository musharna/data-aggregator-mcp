import time
from collections.abc import Awaitable, Callable

import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket, _bucket_for, _ncbi_rate


class FakeClock:
    """Deterministic clock: sleep() advances virtual time instead of waiting. A token
    never needs more than a few waits, so a loop that keeps sleeping (often for 0 s)
    without ever spending one fails here instead of spinning forever."""

    def __init__(self) -> None:
        self.t = 0.0
        self.naps = 0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.naps += 1
        assert self.naps < 50, f"acquire is spinning: slept {self.naps} times, last {dt} s"
        self.t += dt


class InjectedSleepOnly(TokenBucket):
    """A bucket whose default ``sleep`` fails. ``SharedSchedule``'s per-process fallback
    must pass its own sleep down; a bucket left on ``asyncio.sleep`` waits for real on a
    fake clock that never moves, so the test would hang rather than fail."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        async def not_passed_down(dt: float) -> None:
            raise AssertionError(f"waited {dt} s on asyncio.sleep, not the injected sleep")

        super().__init__(rate, capacity, now=now, sleep=sleep or not_passed_down)


@pytest.fixture(autouse=True)
def _reset():
    _ratelimit.reset()
    yield
    _ratelimit.reset()


@pytest.mark.asyncio
async def test_bucket_bursts_to_capacity_then_paces():
    clk = FakeClock()
    b = TokenBucket(rate=3.0, capacity=3.0, now=clk.now, sleep=clk.sleep)
    for _ in range(3):
        await b.acquire()
    assert clk.t == 0.0
    await b.acquire()
    assert clk.t == pytest.approx(1 / 3, abs=1e-6)


_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def test_bucket_classifier_groups_all_ncbi():
    assert _bucket_for("NCBI esearch (geo)", _EUTILS) == "ncbi"
    assert _bucket_for("NCBI efetch (sra)", _EUTILS) == "ncbi"
    idconv = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    assert _bucket_for("NCBI idconv", idconv) == "ncbi"
    assert _bucket_for("Zenodo search", "https://zenodo.org/api/records") == "zenodo-search"
    assert _bucket_for("EuropePMC search", "https://www.ebi.ac.uk/europepmc/x") == "www.ebi.ac.uk"


def test_ncbi_host_is_paced_whatever_the_service_is_called():
    """NCBI throttles per account/IP across its hosts, so the bucket must follow the HOST
    and not our display label for the call. GEO's supplementary listing goes to
    ftp.ncbi.nlm.nih.gov under the label "GEO suppl listing", and drew from the default
    bucket at 10 req/s — over 3x NCBI's keyless ceiling — purely because of its name."""
    geo_suppl = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE100nnn/GSE100866/suppl/"
    assert _bucket_for("GEO suppl listing", geo_suppl) == "ncbi"
    assert _bucket_for("SRA files", "https://trace.ncbi.nlm.nih.gov/Traces/x") == "ncbi"


def test_bucket_host_match_is_not_fooled_by_a_lookalike_host():
    """Suffix matching must be anchored on a dot boundary, or a host merely ENDING in the
    domain text picks up NCBI's pacing — and a real NCBI host could dodge it."""
    assert _bucket_for("x", "https://ncbi.nlm.nih.gov.example.com/a") == (
        "ncbi.nlm.nih.gov.example.com"
    )
    assert _bucket_for("x", "https://notncbi.nlm.nih.gov/a") == "notncbi.nlm.nih.gov"
    assert _bucket_for("x", "https://ncbi.nlm.nih.gov/a") == "ncbi"  # the bare domain counts


def test_bucket_survives_a_url_it_cannot_parse():
    """Pacing must never be the thing that raises: an unparseable URL falls back to the
    label, then to default."""
    assert _bucket_for("x", "") == "default"
    assert _bucket_for("x", "not a url") == "default"
    assert _bucket_for("NCBI esearch (geo)", "not a url") == "ncbi"  # label backstop


def test_ncbi_rate_responds_to_api_key(monkeypatch):
    """Two thirds of NCBI's ceiling: 3/s keyless, 10/s with a key."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _ncbi_rate() == pytest.approx(2.0)
    monkeypatch.setenv("NCBI_API_KEY", "abc")
    assert _ncbi_rate() == pytest.approx(20 / 3)


def test_ncbi_rate_email_only_is_still_keyless(monkeypatch):
    """NCBI grants 10 req/s only with an API key; email alone is identification,
    not elevated access, so the rate stays at the keyless one."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setenv("NCBI_EMAIL", "user@example.com")
    assert _ncbi_rate() == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_acquire_shares_one_bucket_across_ncbi_services():
    await _ratelimit.acquire("NCBI esearch (geo)", _EUTILS)
    await _ratelimit.acquire("NCBI efetch (sra)", _EUTILS)
    assert list(_ratelimit._BUCKETS) == ["ncbi"]


@pytest.mark.asyncio
async def test_geo_suppl_shares_the_ncbi_bucket_with_eutils():
    """The end-to-end form of the fix: one NCBI ceiling, not two independent ones."""
    await _ratelimit.acquire("NCBI esearch (geo)", _EUTILS)
    await _ratelimit.acquire(
        "GEO suppl listing", "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1/suppl/"
    )
    assert list(_ratelimit._BUCKETS) == ["ncbi"]


def test_bucket_survives_a_second_event_loop():
    """L22 (audit 2026-09-22): buckets are module-level and outlive an event loop, but
    each held ONE asyncio.Lock, which binds to the loop of its first contended use. The
    next ``asyncio.run`` (a CLI call, a test, an embedding host) then raised
    ``RuntimeError: ... is bound to a different event loop`` on its first contended
    acquire. The lock is now per loop; the token state stays shared. The first burst is
    the positive control; the pacing assertion proves tokens still carry over."""
    import asyncio

    class YieldingClock(FakeClock):
        async def sleep(self, dt: float) -> None:
            self.t += dt
            await asyncio.sleep(0)  # yield, so the other acquirers contend on the lock

    clk = YieldingClock()
    b = TokenBucket(rate=3.0, capacity=3.0, now=clk.now, sleep=clk.sleep)

    async def burst() -> None:
        await asyncio.gather(*(b.acquire() for _ in range(5)))

    asyncio.run(burst())
    after_first = clk.t
    assert after_first == pytest.approx(2 / 3, abs=1e-6)  # 3 free, 2 paced
    asyncio.run(burst())  # raised RuntimeError before the fix
    # bucket was empty at the start of loop 2, so all 5 are paced: shared state held
    assert clk.t == pytest.approx(after_first + 5 / 3, abs=1e-6)
