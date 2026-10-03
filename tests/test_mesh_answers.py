"""NCBI MeSH answers, each beside the live answer it stands for (probed 2026-10-02).

Every one of 178 live descriptors sampled from esummary db=mesh (version 2.0) carries a
string ``ds_meshui`` and a list of entry-term strings ``ds_meshterms``. A descriptor
without either was read as "no match" and cached for an hour; a string ``ds_meshterms``
was iterated character by character (canonical name ``"A"``); a non-string UI escaped
later as a pydantic error from ``MeshExpansion``.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _ontology, mesh
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ESEARCH = f"{_EUT}/esearch.fcgi?db=mesh&term=asthma%5BMeSH+Terms%5D&retmax=1&retmode=json"
_ESUMMARY = f"{_EUT}/esummary.fcgi?db=mesh&id=68001249&version=2.0&retmode=json"

# Trimmed from the live esummary for "asthma" (uid 68001249), 2026-10-02.
_ASTHMA = {
    "uid": "68001249",
    "ds_recordtype": "descriptor",
    "ds_meshui": "D001249",
    "ds_meshterms": ["Asthma", "Asthmas", "Asthma, Bronchial", "Bronchial Asthma"],
}


def _with(**fields: object) -> dict:
    doc = dict(_ASTHMA)
    for k, v in fields.items():
        if v is _DROP:
            doc.pop(k)
        else:
            doc[k] = v
    return doc


_DROP = object()


@pytest.fixture(autouse=True)
def _clear_mesh_cache(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    mesh._CACHE.clear()
    yield
    mesh._CACHE.clear()


def test_the_live_descriptor_parses() -> None:
    info = mesh._parse_mesh([_ASTHMA])
    assert info == mesh.MeshInfo(
        ui="D001249",
        canonical="Asthma",
        synonyms=("Asthmas", "Asthma, Bronchial", "Bronchial Asthma"),
    )


@pytest.mark.parametrize(
    "doc",
    [
        _with(ds_meshterms=_DROP),
        _with(ds_meshterms=None),
        _with(ds_meshterms=[]),
        _with(ds_meshterms=["", "  "]),
        _with(ds_meshterms="Asthma"),
        _with(ds_meshterms={"0": "Asthma"}),
        _with(ds_meshui=_DROP),
        _with(ds_meshui=None),
        _with(ds_meshui=""),
        _with(ds_meshui=1249),
        _with(ds_meshui=["D001249"]),
    ],
)
def test_a_descriptor_without_a_ui_or_terms_is_an_upstream_failure(doc: dict) -> None:
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] NCBI MeSH answered a descriptor without a "
        r"MeSH UI or entry terms \(ds_meshui=",
    ):
        mesh._parse_mesh([doc])
    assert mesh._parse_mesh([_ASTHMA]) is not None  # positive control


def test_blank_and_non_string_entry_terms_are_skipped_not_taken_as_the_name() -> None:
    info = mesh._parse_mesh([_with(ds_meshterms=["  ", None, 7, "Asthma", "", "Asthmas"])])
    assert info is not None
    assert info.canonical == "Asthma" and info.synonyms == ("Asthmas",)


def test_only_the_first_document_is_read() -> None:
    other = _with(ds_meshui="D000001", ds_meshterms=["Other"])
    info = mesh._parse_mesh([_ASTHMA, other])
    assert info is not None and info.ui == "D001249"
    # a qualifier first is "no match", whatever follows it
    qualifier = _with(ds_recordtype="qualifier", ds_meshui="Q000601", ds_meshterms=["therapy"])
    assert mesh._parse_mesh([qualifier, _ASTHMA]) is None


async def test_a_malformed_descriptor_is_raised_and_not_cached(httpx_mock: HTTPXMock) -> None:
    """Through resolve_mesh: the failure surfaces and nothing is cached, so the next
    call resolves; through expand_disease it lands in ``errors['mesh']`` and the query
    runs un-expanded."""
    found = {"esearchresult": {"count": "1", "idlist": ["68001249"]}}
    httpx_mock.add_response(url=_ESEARCH, json=found)
    bad = _with(ds_meshterms="Asthma")
    httpx_mock.add_response(url=_ESUMMARY, json={"result": {"uids": ["68001249"], "68001249": bad}})
    httpx_mock.add_response(url=_ESEARCH, json=found)
    good = {"result": {"uids": ["68001249"], "68001249": _ASTHMA}}
    httpx_mock.add_response(url=_ESUMMARY, json=good)
    async with httpx.AsyncClient() as client:
        errors: dict[str, str] = {}
        query, echo = await _ontology.expand_disease(client, "q", "asthma", errors)
        assert (query, echo) == ("q", None)
        assert errors == {
            "mesh": "UpstreamUnavailableError: [UpstreamUnavailableError] NCBI MeSH answered "
            "a descriptor without a MeSH UI or entry terms (ds_meshui='D001249', "
            "ds_meshterms='Asthma')"
        }
        assert mesh._CACHE.get("asthma") is mesh.MISS
        info = await mesh.resolve_mesh(client, "asthma")
    assert info is not None and info.ui == "D001249" and info.canonical == "Asthma"


async def test_no_esearch_hit_is_cached_as_no_match_under_the_folded_name(
    httpx_mock: HTTPXMock,
) -> None:
    """Live: ``xyzzyq[MeSH Terms]`` answers count 0 with ``phrasesnotfound``."""
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=mesh&term=+XyzzyQ+%5BMeSH+Terms%5D&retmax=1&retmode=json",
        json={"esearchresult": {"count": "0", "retmax": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        assert await mesh.resolve_mesh(client, " XyzzyQ ") is None
    assert mesh._CACHE.get("xyzzyq") is mesh._NEG  # the key is stripped and lower-cased


@live_only
async def test_live_descriptors_pass_the_check() -> None:
    """The check rejects no live descriptor: the ones the tests and docs name, plus a
    lay name NCBI maps to a different descriptor."""
    async with httpx.AsyncClient() as client:
        for name, ui in [
            ("asthma", "D001249"),
            ("heart attack", "D009203"),
            ("covid-19", "D000086382"),
        ]:
            info = await mesh.resolve_mesh(client, name)
            assert info is not None and info.ui == ui, name
            assert info.canonical and all(info.synonyms), name
