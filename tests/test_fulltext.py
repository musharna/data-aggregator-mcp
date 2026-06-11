from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import fulltext

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


async def test_find_europepmc_xml_when_in_epmc(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=PMCID:"PMC3463246"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "Y", "pmcid": "PMC3463246"}]}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid="PMC3463246", doi="10.7554/eLife.00013")
    assert ft.file is not None
    assert ft.file.source == "europepmc"
    assert ft.file.mime == "application/xml"
    assert ft.file.url == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3463246/fullTextXML"


async def test_find_europepmc_returns_access_and_license(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=PMCID:"PMC3463246"&format=json&resultType=core&pageSize=1',
        json={
            "resultList": {
                "result": [
                    {"inEPMC": "Y", "pmcid": "PMC3463246", "isOpenAccess": "Y", "license": "cc by"}
                ]
            }
        },
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid="PMC3463246", doi="10.7554/eLife.00013")
    assert ft.access == "open"
    assert ft.license == "cc by"


async def test_find_falls_through_to_unpaywall(httpx_mock, monkeypatch) -> None:
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=DOI:"10.1/x"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    httpx_mock.add_response(
        url="https://api.unpaywall.org/v2/10.1/x?email=x@y.z",
        json={"is_oa": True, "best_oa_location": {"url_for_pdf": "https://repo/x.pdf"}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid=None, doi="10.1/x")
    assert ft.file is not None
    assert ft.file.source == "unpaywall"
    assert ft.file.url == "https://repo/x.pdf"
    assert ft.file.mime == "application/pdf"


async def test_find_none_when_no_oa_and_no_email(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=DOI:"10.1/x"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid=None, doi="10.1/x")
    assert ft.file is None  # EPMC miss + Unpaywall skipped (no email)


async def test_find_unpaywall_landing_only_returns_none(httpx_mock, monkeypatch) -> None:
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=DOI:"10.1/g"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    httpx_mock.add_response(
        url="https://api.unpaywall.org/v2/10.1/g?email=x@y.z",
        json={
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": None, "url": "https://pub/landing"},
        },
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid=None, doi="10.1/g")
    assert ft.file is None  # only attach a real PDF, never the HTML landing


async def test_find_fails_soft_on_transport_error(httpx_mock) -> None:
    for _ in range(3):  # request_with_retry retries transport errors max_retries times
        httpx_mock.add_exception(
            httpx.ConnectError("boom"),
            url=f'{_SEARCH}?query=PMCID:"PMC9"&format=json&resultType=core&pageSize=1',
        )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid="PMC9", doi=None)
    assert ft.file is None  # transport error must not raise out of enrichment


async def test_find_europepmc_off_contract_body_fails_soft(httpx_mock) -> None:
    # A 2xx body whose result[0] is not a dict must not raise (.get on a str) — spec §8.
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=PMCID:"PMC1"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": ["not-a-dict"]}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid="PMC1", doi=None)
    assert ft.file is None


async def test_find_unpaywall_off_contract_body_fails_soft(httpx_mock, monkeypatch) -> None:
    # A 2xx Unpaywall body that is a JSON array (not an object) must not raise — spec §8.
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=DOI:"10.3/x"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    httpx_mock.add_response(
        url="https://api.unpaywall.org/v2/10.3/x?email=x@y.z",
        json=["not", "a", "dict"],
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid=None, doi="10.3/x")
    assert ft.file is None


# ---------------------------------------------------------------------------
# Fix — EuropePMC query values must be phrase-quoted
# ---------------------------------------------------------------------------


async def test_europepmc_query_uses_phrase_quotes_for_pmcid(httpx_mock) -> None:
    """The outgoing EuropePMC query must wrap the PMCID value in double quotes.
    pytest-httpx's URL matcher confirms the query param has the quoted form."""
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=PMCID:"PMC3463246"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid="PMC3463246", doi=None)
    assert ft.file is None  # no inEPMC hit, but the key assertion is the URL match above


async def test_europepmc_query_uses_phrase_quotes_for_doi(httpx_mock) -> None:
    """DOI values must also be phrase-quoted in the EuropePMC query."""
    httpx_mock.add_response(
        url=f'{_SEARCH}?query=DOI:"10.1/x"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        ft = await fulltext.find(client, pmcid=None, doi="10.1/x")
    assert ft.file is None  # no inEPMC hit, but the key assertion is the URL match above


@live_only
async def test_live_europepmc_fulltext_for_oa_pmcid() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        ft = await fulltext.find(client, pmcid="PMC3463246", doi="10.7554/eLife.00013")
    assert ft.file is not None and ft.file.source == "europepmc"
