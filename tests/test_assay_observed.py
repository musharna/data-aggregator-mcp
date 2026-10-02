"""What the ``assay`` lookup sends to EBI OLS4 and how it picks an EDAM term, pinned exactly."""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, assay
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test__ols_observed import _TAIL, _UA, _recording
from tests.test_assay_answers import CHIPSEQ


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    async def _no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_wait)
    assay._CACHE.clear()
    yield
    assay._CACHE.clear()


@pytest.mark.asyncio
async def test_the_request_is_exactly_what_ols_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=CHIPSEQ)) as c:
        info = await assay.resolve_edam(c, "  ChIP-seq\t")
    assert info is not None and info.edam_id == "EDAM:topic_3169"
    assert [(r.method, str(r.url)) for r in sent] == [
        ("GET", "https://www.ebi.ac.uk/ols4/api/search?q=ChIP-seq&ontology=edam" + _TAIL),
    ]
    assert [r.headers["User-Agent"] for r in sent] == [_UA]


@pytest.mark.asyncio
async def test_a_failing_search_names_the_edam_lookup_after_two_tries():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] EBI OLS \(EDAM\) exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await assay.resolve_edam(c, "ChIP-seq")
    assert len(sent) == 2
