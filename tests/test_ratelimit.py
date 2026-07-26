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


_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def test_bucket_classifier_groups_all_ncbi():
    assert _bucket_for("NCBI esearch (geo)", _EUTILS) == "ncbi"
    assert _bucket_for("NCBI efetch (sra)", _EUTILS) == "ncbi"
    idconv = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
    assert _bucket_for("NCBI idconv", idconv) == "ncbi"
    assert _bucket_for("Zenodo search", "https://zenodo.org/api/records") == "default"
    assert _bucket_for("EuropePMC search", "https://www.ebi.ac.uk/europepmc/x") == "default"


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
    assert _bucket_for("x", "https://ncbi.nlm.nih.gov.example.com/a") == "default"
    assert _bucket_for("x", "https://notncbi.nlm.nih.gov/a") == "default"
    assert _bucket_for("x", "https://ncbi.nlm.nih.gov/a") == "ncbi"  # the bare domain counts


def test_bucket_survives_a_url_it_cannot_parse():
    """Pacing must never be the thing that raises: an unparseable URL falls back to the
    label, then to default."""
    assert _bucket_for("x", "") == "default"
    assert _bucket_for("x", "not a url") == "default"
    assert _bucket_for("NCBI esearch (geo)", "not a url") == "ncbi"  # label backstop


def test_ncbi_rate_responds_to_api_key(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    assert _rate_for("ncbi") == 3.0
    monkeypatch.setenv("NCBI_API_KEY", "abc")
    assert _rate_for("ncbi") == 10.0


def test_ncbi_rate_email_only_is_still_3(monkeypatch):
    """NCBI grants 10 req/s only with an API key; email alone is identification,
    not elevated access, so the rate must stay at 3 req/s."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setenv("NCBI_EMAIL", "user@example.com")
    from data_aggregator_mcp._ratelimit import _ncbi_rate

    assert _ncbi_rate() == 3.0


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
