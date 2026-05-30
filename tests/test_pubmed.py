from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import pubmed
from data_aggregator_mcp.errors import NotFoundError

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _pubmed_doc() -> dict:
    return {
        "uid": "34320281",
        "title": "Covid-19 Breakthrough Infections in Vaccinated Health Care Workers.",
        "sortpubdate": "2021/10/14 00:00",
        "authors": [{"name": "Bergwerk M"}, {"name": "Gonen T"}],
        "fulljournalname": "The New England journal of medicine",
        "articleids": [
            {"idtype": "pubmed", "value": "34320281"},
            {"idtype": "doi", "value": "10.1056/NEJMoa2109072"},
        ],
    }


def test_normalize_pubmed_maps_core_fields() -> None:
    r = pubmed._normalize_pubmed(_pubmed_doc())
    assert r.id == "pubmed:34320281"
    assert r.source == "pubmed"
    assert r.kind == "publication"
    assert r.title.startswith("Covid-19 Breakthrough")
    assert r.creators == ["Bergwerk M", "Gonen T"]
    assert r.year == 2021
    assert r.doi == "10.1056/NEJMoa2109072"
    assert r.description is None  # esummary has no abstract
    assert r.files == []


def test_normalize_pubmed_without_doi() -> None:
    doc = _pubmed_doc()
    doc["articleids"] = [{"idtype": "pubmed", "value": "1"}]
    doc["uid"] = "1"
    assert pubmed._normalize_pubmed(doc).doi is None


async def test_search_returns_compact_publications(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=covid&retmax=10&retmode=json",
        json={"esearchresult": {"count": "5", "idlist": ["34320281"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id=34320281&version=2.0&retmode=json",
        json={"result": {"uids": ["34320281"], "34320281": _pubmed_doc()}},
    )
    async with httpx.AsyncClient() as client:
        total, results = await pubmed.search(client, "covid")
    assert total == 5
    assert [r.id for r in results] == ["pubmed:34320281"]


def test_normalize_pubmed_doi_entry_missing_value() -> None:
    doc = _pubmed_doc()
    doc["articleids"] = [{"idtype": "doi"}]  # idtype matches but no "value" key
    assert pubmed._normalize_pubmed(doc).doi is None


async def test_search_empty_idlist(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=zzz&retmax=10&retmode=json",
        json={"esearchresult": {"count": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        total, results = await pubmed.search(client, "zzz")
    assert (total, results) == (0, [])


async def test_resolve_attaches_geo_link_via_elink(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=42162664&version=2.0&retmode=json",
        json={"result": {"uids": ["42162664"], "42162664": _pubmed_doc() | {"uid": "42162664"}}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db=sra&id=42162664&retmode=json",
        json={"linksets": [{}]},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db=gds&id=42162664&retmode=json",
        json={"linksets": [{"linksetdbs": [{"dbto": "gds", "links": ["200319641"]}]}]},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db=bioproject&id=42162664&retmode=json",
        json={"linksets": [{}]},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=gds&id=200319641&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["200319641"],
                "200319641": {"accession": "GSE319641", "title": "g", "pdat": "2026/01/01"},
            }
        },
    )
    # full-text discovery: no PMCID on this doc, so EuropePMC is queried by DOI → not in EPMC.
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:10.1056/NEJMoa2109072&format=json&resultType=core&pageSize=1",
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    # abstract enrichment via efetch (no AbstractText → description stays None).
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=42162664&retmode=xml",
        text="<PubmedArticleSet></PubmedArticleSet>",
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:42162664")
    assert r.id == "pubmed:42162664"
    assert [(lnk.rel, lnk.target_id) for lnk in r.links] == [("has_data", "geo:GSE319641")]
    assert r.files == []  # full text not in EPMC → no file attached


async def test_resolve_no_links_when_no_edges(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=1&version=2.0&retmode=json",
        json={"result": {"uids": ["1"], "1": _pubmed_doc() | {"uid": "1"}}},
    )
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db={db}&id=1&retmode=json",
            json={"linksets": [{}]},
        )
    # full-text discovery: no PMCID on this doc, so EuropePMC is queried by DOI → not in EPMC.
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:10.1056/NEJMoa2109072&format=json&resultType=core&pageSize=1",
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    # abstract enrichment via efetch (no AbstractText → description stays None).
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=1&retmode=xml",
        text="<PubmedArticleSet></PubmedArticleSet>",
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:1")
    assert r.links == []
    assert r.files == []


async def test_resolve_unknown_pmid_raises(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=0&version=2.0&retmode=json",
        json={"result": {"uids": []}},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no pubmed record"):
            await pubmed.resolve(client, "pubmed:0")


async def test_resolve_unroutable_prefix_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="unroutable"):
            await pubmed.resolve(client, "openaire:x")


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_resolve_pubmed_attaches_geo_link() -> None:
    # Seed confirmed 2026-05-28: PMID 42162664 elinks to GEO GSE319641.
    # Asserts the MECHANISM (a geo:/sra:/bioproject: link lands), robust to data drift.
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:42162664")
    assert r.id == "pubmed:42162664"
    assert any(lnk.target_id.split(":", 1)[0] in ("geo", "sra", "bioproject") for lnk in r.links)


def test_normalize_populates_identifiers() -> None:
    from data_aggregator_mcp import pubmed

    doc = {
        "uid": "23066504",
        "title": "t",
        "sortpubdate": "2012/10/15 00:00",
        "authors": [],
        "articleids": [
            {"idtype": "pubmed", "value": "23066504"},
            {"idtype": "pmc", "value": "PMC3463246"},
            {"idtype": "doi", "value": "10.7554/eLife.00013"},
        ],
    }
    r = pubmed._normalize_pubmed(doc)
    assert r.doi == "10.7554/eLife.00013"
    assert r.identifiers == {
        "pmid": "23066504",
        "doi": "10.7554/eLife.00013",
        "pmcid": "PMC3463246",
    }


async def test_resolve_populates_abstract(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=42&version=2.0&retmode=json",
        json={"result": {"uids": ["42"], "42": {"uid": "42", "title": "T", "articleids": []}}},
    )
    # elink dbs resolve queries → no links. No doi/pmcid on this doc, so fulltext.find
    # early-returns with no HTTP (no EuropePMC mock needed).
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db={db}&id=42&retmode=json",
            json={"linksets": [{}]},
        )
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=42&retmode=xml",
        text=(
            "<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>"
            "<AbstractText>First part.</AbstractText>"
            '<AbstractText Label="METHODS">Second part.</AbstractText>'
            "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ),
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:42")
    assert r.description is not None
    assert "First part." in r.description and "Second part." in r.description


async def test_resolve_abstract_keeps_inline_markup_tail(httpx_mock, monkeypatch) -> None:
    # AbstractText with inline markup (<sub>, common in MEDLINE XML): el.text alone would
    # truncate at the first child ("We measured CO"); itertext() must keep the full string.
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=42&version=2.0&retmode=json",
        json={"result": {"uids": ["42"], "42": {"uid": "42", "title": "T", "articleids": []}}},
    )
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db={db}&id=42&retmode=json",
            json={"linksets": [{}]},
        )
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=42&retmode=xml",
        text=(
            "<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>"
            "<AbstractText>We measured CO<sub>2</sub> uptake at 25<sup>o</sup>C.</AbstractText>"
            "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ),
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:42")
    assert r.description == "We measured CO2 uptake at 25oC."


@live_only
async def test_live_resolve_pubmed_has_abstract() -> None:
    from data_aggregator_mcp import pubmed

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await pubmed.resolve(client, "pubmed:23066504")
    assert r.description and len(r.description) > 40


async def test_resolve_attaches_oa_fulltext(httpx_mock, monkeypatch) -> None:
    # Avoid real network for elink: stub the data-link discovery to empty.
    async def _no_links(client, pmid):
        return []

    monkeypatch.setattr("data_aggregator_mcp.pubmed._links_via_elink", _no_links)
    # esummary for the PMID (carries the pmc/doi articleids).
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id=23066504&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["23066504"],
                "23066504": {
                    "uid": "23066504",
                    "title": "t",
                    "sortpubdate": "2012/10/15 00:00",
                    "authors": [],
                    "articleids": [
                        {"idtype": "pmc", "value": "PMC3463246"},
                        {"idtype": "doi", "value": "10.7554/eLife.00013"},
                    ],
                },
            }
        },
    )
    # EuropePMC existence check → in EPMC.
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:PMC3463246&format=json&resultType=core&pageSize=1",
        json={"resultList": {"result": [{"inEPMC": "Y", "pmcid": "PMC3463246"}]}},
    )
    # abstract enrichment via efetch (no AbstractText → description stays None).
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=23066504&retmode=xml",
        text="<PubmedArticleSet></PubmedArticleSet>",
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:23066504")
    assert len(r.files) == 1
    assert r.files[0].source == "europepmc"
    assert r.identifiers["pmcid"] == "PMC3463246"


async def test_resolve_pubmed_access_from_europepmc(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp import pubmed

    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    eut = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    httpx_mock.add_response(
        url=f"{eut}/esummary.fcgi?db=pubmed&id=7&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["7"],
                "7": {
                    "uid": "7",
                    "title": "T",
                    "articleids": [{"idtype": "pmc", "value": "PMC7"}],
                },
            }
        },
    )
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{eut}/elink.fcgi?dbfrom=pubmed&db={db}&id=7&retmode=json",
            json={"linksets": [{}]},
        )
    httpx_mock.add_response(
        url=f"{eut}/efetch.fcgi?db=pubmed&id=7&retmode=xml",
        text="<PubmedArticleSet/>",
    )
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:PMC7&format=json&resultType=core&pageSize=1",
        json={
            "resultList": {
                "result": [{"inEPMC": "Y", "pmcid": "PMC7", "isOpenAccess": "Y", "license": "cc0"}]
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await pubmed.resolve(client, "pubmed:7")
    assert r.access == "open"
    assert r.license == "cc0"
    assert r.files and r.files[0].source == "europepmc"
