from __future__ import annotations

import httpx
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _eutils


async def test_esearch_returns_count_and_idlist(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=rice&retmax=10&retmode=json",
        json={"esearchresult": {"count": "42", "idlist": ["200181578", "200181579"]}},
    )
    async with httpx.AsyncClient() as client:
        count, ids = await _eutils.esearch(client, "gds", "rice", retmax=10)
    assert count == 42
    assert ids == ["200181578", "200181579"]


async def test_esummary_returns_docs_in_idlist_order(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1,2&version=2.0&retmode=json",
        json={
            "result": {"uids": ["1", "2"], "1": {"accession": "GSE1"}, "2": {"accession": "GSE2"}}
        },
    )
    async with httpx.AsyncClient() as client:
        docs = await _eutils.esummary(client, "gds", ["1", "2"])
    assert [d["accession"] for d in docs] == ["GSE1", "GSE2"]


async def test_esummary_empty_ids_returns_empty(monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        assert await _eutils.esummary(client, "gds", []) == []


async def test_api_key_appended_when_env_set(httpx_mock: HTTPXMock, monkeypatch) -> None:
    # The mock matches only if _common_params() appended api_key=testkey to the URL.
    monkeypatch.setenv("NCBI_API_KEY", "testkey")
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=rice&retmax=10&retmode=json&api_key=testkey",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    async with httpx.AsyncClient() as client:
        _count, ids = await _eutils.esearch(client, "gds", "rice", retmax=10)
    assert ids == ["1"]


async def test_elink_collects_links_across_linksetdbs(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=gds&id=42162664&retmode=json",
        json={
            "linksets": [
                {
                    "linksetdbs": [
                        {
                            "dbto": "gds",
                            "linkname": "pubmed_gds",
                            "links": ["200319641", "200319642"],
                        }
                    ]
                }
            ]
        },
    )
    async with httpx.AsyncClient() as client:
        uids = await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664"])
    assert uids == ["200319641", "200319642"]


async def test_elink_empty_when_no_linksetdbs(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=sra&id=99&retmode=json",
        json={"linksets": [{}]},
    )
    async with httpx.AsyncClient() as client:
        assert await _eutils.elink(client, dbfrom="pubmed", db="sra", ids=["99"]) == []


async def test_elink_empty_ids_returns_empty(monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        assert await _eutils.elink(client, dbfrom="pubmed", db="sra", ids=[]) == []


async def test_efetch_returns_text_body(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=taxonomy&id=99112&retmode=xml",
        text="<TaxaSet><Taxon><TaxId>99112</TaxId></Taxon></TaxaSet>",
    )
    async with httpx.AsyncClient() as client:
        body = await _eutils.efetch(client, "taxonomy", ["99112"])
    assert "<TaxId>99112</TaxId>" in body


async def test_efetch_empty_ids_returns_empty_string() -> None:
    async with httpx.AsyncClient() as client:
        assert await _eutils.efetch(client, "taxonomy", []) == ""


async def test_efetch_appends_api_key(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.setenv("NCBI_API_KEY", "testkey")
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=taxonomy&id=99112&retmode=xml&api_key=testkey",
        text="<ok/>",
    )
    async with httpx.AsyncClient() as client:
        assert "<ok/>" in await _eutils.efetch(client, "taxonomy", ["99112"])


async def test_efetch_retries_malformed_xml_then_returns_text(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=taxonomy&id=3701&retmode=xml"
    )
    httpx_mock.add_response(url=url, text="<TaxaSet")  # truncated
    httpx_mock.add_response(url=url, text="<TaxaSet><Taxon><TaxId>3701</TaxId></Taxon></TaxaSet>")
    async with httpx.AsyncClient() as client:
        body = await _eutils.efetch(client, "taxonomy", ["3701"])
    assert "<TaxId>3701</TaxId>" in body
