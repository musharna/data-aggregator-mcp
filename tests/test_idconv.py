from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import idconv

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"


async def test_identifiers_for_maps_doi(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f"{_URL}?ids=10.7554/eLife.00013&format=json&tool=data-aggregator-mcp",
        json={"records": [{"doi": "10.7554/eLife.00013", "pmcid": "PMC3463246", "pmid": 23066504}]},
    )
    async with httpx.AsyncClient() as client:
        out = await idconv.identifiers_for(client, "10.7554/eLife.00013")
    assert out == {"doi": "10.7554/eLife.00013", "pmid": "23066504", "pmcid": "PMC3463246"}


async def test_identifiers_for_empty_doi_makes_no_call() -> None:
    async with httpx.AsyncClient() as client:
        assert await idconv.identifiers_for(client, "") == {}


async def test_identifiers_for_error_record_returns_empty(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f"{_URL}?ids=10.0/bad&format=json&tool=data-aggregator-mcp",
        json={"records": [{"status": "error", "errmsg": "invalid article id"}]},
    )
    async with httpx.AsyncClient() as client:
        assert await idconv.identifiers_for(client, "10.0/bad") == {}


async def test_identifiers_for_fails_soft_on_transport_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    for _ in range(3):  # request_with_retry retries transport errors max_retries times
        httpx_mock.add_exception(
            httpx.ConnectError("boom"),
            url=f"{_URL}?ids=10.1/x&format=json&tool=data-aggregator-mcp",
        )
    async with httpx.AsyncClient() as client:
        assert await idconv.identifiers_for(client, "10.1/x") == {}  # never raises


async def test_identifiers_for_off_contract_body_fails_soft(httpx_mock, monkeypatch) -> None:
    # A 2xx body whose records[0] is not a dict must not raise (.get on a str) — spec §8.
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f"{_URL}?ids=10.2/x&format=json&tool=data-aggregator-mcp",
        json={"records": ["not-a-dict"]},
    )
    async with httpx.AsyncClient() as client:
        assert await idconv.identifiers_for(client, "10.2/x") == {}


@live_only
async def test_live_idconv_roundtrip() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        out = await idconv.identifiers_for(client, "10.7554/eLife.00013")
    assert out.get("pmid") == "23066504"
    assert out.get("pmcid") == "PMC3463246"
