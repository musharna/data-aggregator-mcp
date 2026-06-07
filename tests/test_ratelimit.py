import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import TokenBucket, _bucket_for, _rate_for


class FakeClock:
    """Deterministic clock: sleep() advances virtual time instead of waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


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


def test_bucket_classifier_groups_all_ncbi():
    assert _bucket_for("NCBI esearch (geo)") == "ncbi"
    assert _bucket_for("NCBI efetch (sra)") == "ncbi"
    assert _bucket_for("NCBI idconv") == "ncbi"
    assert _bucket_for("Zenodo search") == "default"
    assert _bucket_for("EuropePMC search") == "default"


def test_ncbi_rate_responds_to_api_key(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _rate_for("ncbi") == 3.0
    monkeypatch.setenv("NCBI_API_KEY", "abc")
    assert _rate_for("ncbi") == 10.0


@pytest.mark.asyncio
async def test_acquire_shares_one_bucket_across_ncbi_services():
    await _ratelimit.acquire("NCBI esearch (geo)")
    await _ratelimit.acquire("NCBI efetch (sra)")
    assert list(_ratelimit._BUCKETS) == ["ncbi"]
