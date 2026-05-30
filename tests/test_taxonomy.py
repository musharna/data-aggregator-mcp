from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import taxonomy

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_TAXON_XML = (
    "<TaxaSet><Taxon>"
    "<TaxId>99112</TaxId>"
    "<ScientificName>Phelipanche aegyptiaca</ScientificName>"
    "<OtherNames><Synonym>Orobanche aegyptiaca</Synonym>"
    "<Synonym>Phelypaea aegyptiaca</Synonym></OtherNames>"
    "<Lineage>cellular organisms; Eukaryota; Viridiplantae; Streptophyta</Lineage>"
    "</Taxon></TaxaSet>"
)

_NON_PLANT_XML = (
    "<TaxaSet><Taxon>"
    "<TaxId>9606</TaxId>"
    "<ScientificName>Homo sapiens</ScientificName>"
    "<OtherNames><Synonym>human</Synonym></OtherNames>"
    "<Lineage>cellular organisms; Eukaryota; Metazoa; Chordata</Lineage>"
    "</Taxon></TaxaSet>"
)


def test_parse_taxon_extracts_canonical_synonyms_and_plant_flag() -> None:
    info = taxonomy._parse_taxon(_TAXON_XML)
    assert info is not None
    assert info.taxid == 99112
    assert info.canonical_name == "Phelipanche aegyptiaca"
    assert info.synonyms == ("Orobanche aegyptiaca", "Phelypaea aegyptiaca")
    assert info.is_plant is True


def test_parse_taxon_non_plant_lineage() -> None:
    info = taxonomy._parse_taxon(_NON_PLANT_XML)
    assert info is not None and info.is_plant is False


def test_parse_taxon_empty_document_returns_none() -> None:
    assert taxonomy._parse_taxon("<TaxaSet></TaxaSet>") is None


@pytest.fixture(autouse=True)
def _clear_taxonomy_cache():
    taxonomy._CACHE.clear()
    yield
    taxonomy._CACHE.clear()


async def test_resolve_taxon_hits_esearch_then_efetch(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=taxonomy&term=Arabidopsis&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["3701"]}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=taxonomy&id=3701&retmode=xml",
        text=(
            "<TaxaSet><Taxon><TaxId>3701</TaxId>"
            "<ScientificName>Arabidopsis</ScientificName>"
            "<OtherNames><Synonym>Arabis</Synonym></OtherNames>"
            "<Lineage>cellular organisms; Eukaryota; Viridiplantae</Lineage>"
            "</Taxon></TaxaSet>"
        ),
    )
    async with httpx.AsyncClient() as client:
        info = await taxonomy.resolve_taxon(client, "Arabidopsis")
    assert info is not None
    assert info.taxid == 3701 and info.is_plant is True


async def test_resolve_taxon_no_match_returns_none_and_caches(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=taxonomy&term=notaspecies&retmax=1&retmode=json",
        json={"esearchresult": {"count": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        assert await taxonomy.resolve_taxon(client, "notaspecies") is None
        # second call must NOT issue a second esearch (negative result cached)
        assert await taxonomy.resolve_taxon(client, "NotASpecies") is None
    assert len(httpx_mock.get_requests()) == 1


async def test_resolve_taxon_blank_name_returns_none_without_request(httpx_mock: HTTPXMock) -> None:
    async with httpx.AsyncClient() as client:
        assert await taxonomy.resolve_taxon(client, "   ") is None
    assert httpx_mock.get_requests() == []


async def test_resolve_taxon_caches_positive_hit(httpx_mock: HTTPXMock, monkeypatch) -> None:
    # a successful resolve is cached: the second call issues NO further HTTP
    # (neither esearch nor efetch) — total requests stays at 2.
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=taxonomy&term=Arabidopsis&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["3701"]}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=taxonomy&id=3701&retmode=xml",
        text=(
            "<TaxaSet><Taxon><TaxId>3701</TaxId>"
            "<ScientificName>Arabidopsis</ScientificName>"
            "<OtherNames><Synonym>Arabis</Synonym></OtherNames>"
            "<Lineage>cellular organisms; Eukaryota; Viridiplantae</Lineage>"
            "</Taxon></TaxaSet>"
        ),
    )
    async with httpx.AsyncClient() as client:
        first = await taxonomy.resolve_taxon(client, "Arabidopsis")
        second = await taxonomy.resolve_taxon(client, "ARABIDOPSIS")  # case-different → same key
    assert first is not None and first.taxid == 3701
    assert second is first  # exact cached object, no re-resolve
    assert len(httpx_mock.get_requests()) == 2  # esearch + efetch once total, not 4


@live_only
async def test_live_resolve_taxon_phelipanche_synonym() -> None:
    # The documented blind-spot: both names → taxid 99112; the other is a synonym.
    taxonomy._CACHE.clear()
    async with httpx.AsyncClient() as client:
        info = await taxonomy.resolve_taxon(client, "Orobanche aegyptiaca")
    assert info is not None
    assert info.taxid == 99112
    assert info.canonical_name == "Phelipanche aegyptiaca"
    assert "Orobanche aegyptiaca" in info.synonyms
    assert info.is_plant is True
