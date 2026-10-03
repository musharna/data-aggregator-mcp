"""NCBI Taxonomy answers, each beside the live answer it stands for (probed 2026-10-02).

efetch db=taxonomy answers an id with no record with HTTP 200 and an empty
``<TaxaSet>`` (id 999999999 or -5): that is "no such taxon". It refuses an id it cannot
read with HTTP 400 and an ``<eFetchResult><ERROR>`` body (id 0, ``abc``), which the HTTP
layer already raises. The same envelope, or a ``<Taxon>`` without its id or name, inside
a 200 is an off-contract answer; ``_parse_taxon`` read both as "no such taxon", and
``resolve_taxon`` cached that for an hour.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _eutils, taxonomy
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH = f"{_EUT}/esearch.fcgi?db=taxonomy&term=Arabidopsis&retmax=1&retmode=json"
_EFETCH = f"{_EUT}/efetch.fcgi?db=taxonomy&id=3701&retmode=xml"

# Verbatim live answers (2026-10-02).
_EMPTY_TAXASET = (
    '<?xml version="1.0" ?>\n'
    '<!DOCTYPE TaxaSet PUBLIC "-//NLM//DTD Taxon, 14th January 2002//EN" '
    '"https://www.ncbi.nlm.nih.gov/entrez/query/DTD/taxon.dtd">\n'
    "<TaxaSet>\n</TaxaSet>"
)
_EFETCH_ERROR = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n'
    '<!DOCTYPE eEfetchResult PUBLIC "-//NLM//DTD efetch 20131226//EN" '
    '"https://eutils.ncbi.nlm.nih.gov/eutils/dtd/20131226/efetch.dtd">\n'
    "<eFetchResult>\n\t<ERROR>ID list is empty! Possibly it has no correct IDs.</ERROR>\n"
    "</eFetchResult>"
)
_ARABIDOPSIS = (
    "<TaxaSet><Taxon><TaxId>3701</TaxId><ScientificName>Arabidopsis</ScientificName>"
    "<Lineage>cellular organisms; Eukaryota; Viridiplantae</Lineage></Taxon></TaxaSet>"
)


@pytest.fixture(autouse=True)
def _clear_taxonomy_cache(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    taxonomy._CACHE.clear()
    yield
    taxonomy._CACHE.clear()


def test_an_empty_taxaset_is_no_such_taxon() -> None:
    assert taxonomy._parse_taxon(_EMPTY_TAXASET) is None


def test_an_efetch_error_envelope_is_an_upstream_failure_not_a_missing_taxon() -> None:
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] NCBI taxonomy efetch answered <eFetchResult>, not <TaxaSet>: "
        r"'ID list is empty! Possibly it has no correct IDs\.'$",
    ):
        taxonomy._parse_taxon(_EFETCH_ERROR)
    info = taxonomy._parse_taxon(_ARABIDOPSIS)  # positive control
    assert info is not None and info.taxid == 3701


@pytest.mark.parametrize(
    ("taxon", "named"),
    [
        (
            "<ScientificName>Arabidopsis</ScientificName>",
            "TaxId=None, ScientificName='Arabidopsis'",
        ),
        ("<TaxId>3701</TaxId>", "TaxId='3701', ScientificName=None"),
        ("<TaxId></TaxId><ScientificName>Arabidopsis</ScientificName>", "TaxId=''"),
        ("<TaxId>3701</TaxId><ScientificName></ScientificName>", "ScientificName=''"),
    ],
)
def test_a_taxon_without_its_id_or_name_is_an_upstream_failure(taxon: str, named: str) -> None:
    with pytest.raises(UpstreamUnavailableError, match="without a TaxId or ScientificName") as exc:
        taxonomy._parse_taxon(f"<TaxaSet><Taxon>{taxon}</Taxon></TaxaSet>")
    assert named in str(exc.value)
    assert taxonomy._parse_taxon(_ARABIDOPSIS) is not None  # positive control


async def test_an_efetch_error_is_raised_and_not_cached_as_no_match(httpx_mock: HTTPXMock) -> None:
    """Through resolve_taxon: the failure surfaces (the router records it in
    ``errors['taxonomy']``) and nothing is cached, so the next call resolves."""
    httpx_mock.add_response(
        url=_ESEARCH, json={"esearchresult": {"count": "1", "idlist": ["3701"]}}
    )
    httpx_mock.add_response(url=_EFETCH, text=_EFETCH_ERROR)
    httpx_mock.add_response(
        url=_ESEARCH, json={"esearchresult": {"count": "1", "idlist": ["3701"]}}
    )
    httpx_mock.add_response(url=_EFETCH, text=_ARABIDOPSIS)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="not <TaxaSet>"):
            await taxonomy.resolve_taxon(client, "Arabidopsis")
        assert taxonomy._CACHE.get("arabidopsis") is taxonomy.MISS
        info = await taxonomy.resolve_taxon(client, "Arabidopsis")
    assert info is not None and info.taxid == 3701 and info.is_plant


async def test_an_empty_taxaset_is_cached_as_no_match(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_ESEARCH, json={"esearchresult": {"count": "1", "idlist": ["3701"]}}
    )
    httpx_mock.add_response(url=_EFETCH, text=_EMPTY_TAXASET)
    async with httpx.AsyncClient() as client:
        assert await taxonomy.resolve_taxon(client, "Arabidopsis") is None
        assert await taxonomy.resolve_taxon(client, " arabidopsis ") is None  # cached
    assert len(httpx_mock.get_requests()) == 2
    assert taxonomy._CACHE.get("arabidopsis") is taxonomy._NEG


async def test_no_esearch_hit_is_cached_as_no_match_under_the_folded_name(
    httpx_mock: HTTPXMock,
) -> None:
    """Live: esearch for ``yeast`` answers count 0 (NCBI Taxonomy has no such name)."""
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=taxonomy&term=+Yeast+&retmax=1&retmode=json",
        json={"esearchresult": {"count": "0", "retmax": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        assert await taxonomy.resolve_taxon(client, " Yeast ") is None
    assert taxonomy._CACHE.get("yeast") is taxonomy._NEG  # the key is stripped and lower-cased


def test_a_taxon_without_a_lineage_is_not_a_plant() -> None:
    info = taxonomy._parse_taxon(
        _ARABIDOPSIS.replace("<Lineage>cellular organisms; Eukaryota; Viridiplantae</Lineage>", "")
    )
    assert info is not None and info.is_plant is False
    assert taxonomy._parse_taxon(_ARABIDOPSIS).is_plant is True  # positive control


@live_only
async def test_live_efetch_answers_are_read_as_probed() -> None:
    """The answers above, asked of NCBI: a real taxon, an id with no record (an empty
    set: no match), and an id efetch refuses (HTTP 400 with the error envelope: a
    failure, raised before ``_parse_taxon`` sees it)."""
    async with httpx.AsyncClient() as client:
        real = await _eutils.efetch(client, "taxonomy", ["3702"], retmode="xml")
        empty = await _eutils.efetch(client, "taxonomy", ["999999999"], retmode="xml")
        with pytest.raises(UpstreamUnavailableError, match=r"(?s)HTTP 400: .*<eFetchResult>"):
            await _eutils.efetch(client, "taxonomy", ["0"], retmode="xml")
    info = taxonomy._parse_taxon(real)
    assert info is not None and info.taxid == 3702 and info.is_plant
    assert info.canonical_name == "Arabidopsis thaliana"
    assert taxonomy._parse_taxon(empty) is None
