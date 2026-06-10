from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import mesh
from data_aggregator_mcp._cache import TTLCache

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _summary(
    uid: str,
    *,
    recordtype: str = "descriptor",
    meshui: str = "D001943",
    meshterms: list[str] | None = None,
) -> dict:
    doc: dict = {"uid": uid, "ds_recordtype": recordtype, "ds_meshui": meshui}
    if meshterms is not None:
        doc["ds_meshterms"] = meshterms
    return {"result": {"uids": [uid], uid: doc}}


def test_parse_mesh_extracts_ui_canonical_and_synonyms() -> None:
    docs = [
        {
            "ds_recordtype": "descriptor",
            "ds_meshui": "D001943",
            "ds_meshterms": ["Breast Neoplasms", "Breast Cancer", "Breast Tumors"],
        }
    ]
    info = mesh._parse_mesh(docs)
    assert info is not None
    assert info.ui == "D001943"
    assert info.canonical == "Breast Neoplasms"
    assert info.synonyms == ("Breast Cancer", "Breast Tumors")


def test_parse_mesh_rejects_non_descriptor() -> None:
    docs = [
        {
            "ds_recordtype": "qualifier",
            "ds_meshui": "Q000601",
            "ds_meshterms": ["therapy"],
        }
    ]
    assert mesh._parse_mesh(docs) is None


def test_parse_mesh_empty_terms_returns_none() -> None:
    docs = [{"ds_recordtype": "descriptor", "ds_meshui": "D001943", "ds_meshterms": []}]
    assert mesh._parse_mesh(docs) is None
    docs2 = [{"ds_recordtype": "descriptor", "ds_meshui": "D001943"}]
    assert mesh._parse_mesh(docs2) is None


def test_parse_mesh_missing_ui_returns_none() -> None:
    docs = [{"ds_recordtype": "descriptor", "ds_meshui": "", "ds_meshterms": ["Breast Neoplasms"]}]
    assert mesh._parse_mesh(docs) is None


def test_parse_mesh_empty_docs_returns_none() -> None:
    assert mesh._parse_mesh([]) is None


@pytest.fixture(autouse=True)
def _clear_mesh_cache():
    mesh._CACHE.clear()
    yield
    mesh._CACHE.clear()


async def test_resolve_mesh_hits_esearch_then_esummary(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=mesh&term=breast+cancer%5BMeSH+Terms%5D&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["68001943"]}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=mesh&id=68001943&version=2.0&retmode=json",
        json=_summary(
            "68001943",
            meshterms=["Breast Neoplasms", "Breast Cancer"],
        ),
    )
    async with httpx.AsyncClient() as client:
        info = await mesh.resolve_mesh(client, "breast cancer")
    assert info is not None
    assert info.ui == "D001943"
    assert info.canonical == "Breast Neoplasms"
    assert info.synonyms == ("Breast Cancer",)


async def test_resolve_mesh_no_match_returns_none_and_caches(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=mesh&term=notadisease%5BMeSH+Terms%5D&retmax=1&retmode=json",
        json={"esearchresult": {"count": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        assert await mesh.resolve_mesh(client, "notadisease") is None
        # negative result cached: no second esearch
        assert await mesh.resolve_mesh(client, "NotADisease") is None
    assert len(httpx_mock.get_requests()) == 1


async def test_resolve_mesh_blank_name_returns_none_without_request(httpx_mock: HTTPXMock) -> None:
    async with httpx.AsyncClient() as client:
        assert await mesh.resolve_mesh(client, "   ") is None
    assert httpx_mock.get_requests() == []


async def test_resolve_mesh_non_descriptor_caches_negative(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=mesh&term=therapy%5BMeSH+Terms%5D&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["85000601"]}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=mesh&id=85000601&version=2.0&retmode=json",
        json=_summary("85000601", recordtype="qualifier", meshterms=["therapy"]),
    )
    async with httpx.AsyncClient() as client:
        assert await mesh.resolve_mesh(client, "therapy") is None
        # negative cached; second call issues no further HTTP
        assert await mesh.resolve_mesh(client, "therapy") is None
    assert len(httpx_mock.get_requests()) == 2


async def test_resolve_mesh_caches_positive_hit(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url=f"{_EUT}/esearch.fcgi?db=mesh&term=breast+cancer%5BMeSH+Terms%5D&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["68001943"]}},
    )
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=mesh&id=68001943&version=2.0&retmode=json",
        json=_summary("68001943", meshterms=["Breast Neoplasms", "Breast Cancer"]),
    )
    async with httpx.AsyncClient() as client:
        first = await mesh.resolve_mesh(client, "breast cancer")
        second = await mesh.resolve_mesh(client, "Breast Cancer")  # case-different → same key
    assert first is not None and first.ui == "D001943"
    assert second is first
    assert len(httpx_mock.get_requests()) == 2  # esearch + esummary once, not 4


@pytest.mark.asyncio
async def test_resolve_mesh_http_error_propagates_and_not_cached(monkeypatch) -> None:
    mesh._CACHE.clear()
    calls = {"n": 0}

    async def boom_esearch(client, db, term, *, retmax=1):
        calls["n"] += 1
        raise httpx.ConnectError("NCBI down")

    monkeypatch.setattr(mesh._eutils, "esearch", boom_esearch)
    with pytest.raises(httpx.ConnectError):
        await mesh.resolve_mesh(None, "breast cancer")
    # not cached: a second call retries the lookup
    with pytest.raises(httpx.ConnectError):
        await mesh.resolve_mesh(None, "breast cancer")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_resolve_mesh_negative_cache_one_roundtrip(monkeypatch) -> None:
    mesh._CACHE.clear()
    calls = {"n": 0}

    async def fake_esearch(client, db, term, *, retmax=1):
        calls["n"] += 1
        return 0, []

    monkeypatch.setattr(mesh._eutils, "esearch", fake_esearch)
    assert await mesh.resolve_mesh(None, "Nonexistus") is None
    assert await mesh.resolve_mesh(None, "Nonexistus") is None
    assert calls["n"] == 1


class _Clk:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_resolve_mesh_cache_expires(monkeypatch) -> None:
    clk = _Clk()
    saved = mesh._CACHE
    mesh._CACHE = TTLCache(maxsize=64, ttl=10.0, now=clk.now)
    try:
        calls = {"n": 0}

        async def fake_esearch(client, db, term, *, retmax=1):
            calls["n"] += 1
            return 0, []

        monkeypatch.setattr(mesh._eutils, "esearch", fake_esearch)
        await mesh.resolve_mesh(None, "X")
        clk.t = 10.0  # expire
        await mesh.resolve_mesh(None, "X")
        assert calls["n"] == 2
    finally:
        mesh._CACHE = saved


@live_only
async def test_live_resolve_mesh_breast_cancer() -> None:
    # Real-execution boundary check (contract from the plan): the canonical MeSH
    # descriptor for "breast cancer" is D001943 / "Breast Neoplasms". If NCBI's
    # canonical surface form drifts, fix THIS assertion, not the code.
    mesh._CACHE.clear()
    async with httpx.AsyncClient() as client:
        info = await mesh.resolve_mesh(client, "breast cancer")
    assert info is not None
    assert info.ui == "D001943"
    assert info.canonical == "Breast Neoplasms"
    assert len(info.synonyms) > 0
