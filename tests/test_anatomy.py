from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import anatomy
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_OLS = "https://www.ebi.ac.uk/ols4/api/search"


def _doc(
    *,
    obo_id: str | None = "UBERON:0002107",
    label: str | None = "liver",
    synonym: list[str] | None = None,
    is_defining_ontology: bool | None = None,
    is_obsolete: bool | None = None,
) -> dict:
    doc: dict = {}
    if obo_id is not None:
        doc["obo_id"] = obo_id
    if label is not None:
        doc["label"] = label
    if synonym is not None:
        doc["synonym"] = synonym
    if is_defining_ontology is not None:
        doc["is_defining_ontology"] = is_defining_ontology
    if is_obsolete is not None:
        doc["is_obsolete"] = is_obsolete
    return doc


def _envelope(docs: list[dict]) -> dict:
    return {"response": {"numFound": len(docs), "docs": docs}}


# --- _pick_uberon unit table -------------------------------------------------


def test_pick_uberon_exact_label_hit() -> None:
    docs = [_doc(label="liver", synonym=["iecur", "jecur"], is_defining_ontology=True)]
    info = anatomy._pick_uberon(docs, "liver")
    assert info is not None
    assert info.uberon_id == "UBERON:0002107"
    assert info.canonical == "liver"
    assert info.synonyms == ("iecur", "jecur")


def test_pick_uberon_synonym_exact_hit() -> None:
    # input matches a synonym, not the canonical label (case-insensitive).
    docs = [_doc(label="liver", synonym=["Iecur"])]
    info = anatomy._pick_uberon(docs, "iecur")
    assert info is not None
    assert info.canonical == "liver"
    assert info.uberon_id == "UBERON:0002107"


def test_pick_uberon_rejects_non_uberon_obo_id_even_if_label_matches() -> None:
    # cross-ontology leak guard: q=hepar leaks PR: (Protein Ontology) terms.
    docs = [_doc(obo_id="PR:000050567", label="liver", synonym=["iecur"])]
    assert anatomy._pick_uberon(docs, "liver") is None


def test_pick_uberon_rejects_obsolete() -> None:
    docs = [_doc(label="liver", is_obsolete=True)]
    assert anatomy._pick_uberon(docs, "liver") is None


def test_pick_uberon_no_exact_match_returns_none() -> None:
    # top relevance hit is a narrower term, not the canonical → conservative None.
    docs = [_doc(obo_id="UBERON:0001117", label="caudate lobe of liver")]
    assert anatomy._pick_uberon(docs, "liver") is None


def test_pick_uberon_tolerates_absent_synonym_and_defining_fields() -> None:
    docs = [_doc(label="liver")]  # no synonym, is_defining_ontology, is_obsolete
    info = anatomy._pick_uberon(docs, "liver")
    assert info is not None
    assert info.canonical == "liver"
    assert info.synonyms == ()


def test_pick_uberon_prefers_defining_ontology_tiebreak() -> None:
    docs = [
        _doc(obo_id="UBERON:0000001", label="liver", is_defining_ontology=False),
        _doc(obo_id="UBERON:0002107", label="liver", is_defining_ontology=True),
    ]
    info = anatomy._pick_uberon(docs, "liver")
    assert info is not None
    assert info.uberon_id == "UBERON:0002107"


def test_pick_uberon_drops_blank_synonyms() -> None:
    docs = [_doc(label="liver", synonym=["iecur", "  ", 123, "jecur"])]
    info = anatomy._pick_uberon(docs, "liver")
    assert info is not None
    assert info.synonyms == ("iecur", "jecur")


def test_pick_uberon_scalar_synonym_not_char_exploded() -> None:
    # OLS (Solr) can return a single-valued `synonym` as a bare string. Iterating
    # it would explode "iecur" into ('i','e','c','u','r') AND break the synonym
    # match. Build the doc directly (bypass the list-typed _doc helper).
    docs = [{"obo_id": "UBERON:0002107", "label": "liver", "synonym": "iecur"}]
    # (a) synonyms tuple is the whole string, not its characters
    by_label = anatomy._pick_uberon(docs, "liver")
    assert by_label is not None
    assert by_label.synonyms == ("iecur",)
    # (b) a synonym-input still matches (membership not destroyed)
    by_syn = anatomy._pick_uberon(docs, "iecur")
    assert by_syn is not None
    assert by_syn.uberon_id == "UBERON:0002107"


def test_pick_uberon_empty_docs_returns_none() -> None:
    assert anatomy._pick_uberon([], "liver") is None


# --- resolve_uberon ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_anatomy_cache():
    anatomy._CACHE.clear()
    yield
    anatomy._CACHE.clear()


async def test_resolve_uberon_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_OLS
        + "?q=liver&ontology=uberon&exact=true"
        + "&fieldList=obo_id%2Clabel%2Csynonym%2Cis_defining_ontology%2Cis_obsolete&rows=10",
        json=_envelope([_doc(label="liver", synonym=["iecur", "jecur"])]),
    )
    async with httpx.AsyncClient() as client:
        info = await anatomy.resolve_uberon(client, "liver")
    assert info is not None
    assert info.uberon_id == "UBERON:0002107"
    assert info.canonical == "liver"
    assert info.synonyms == ("iecur", "jecur")


async def test_resolve_uberon_no_match_returns_none_and_caches(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([]))
    async with httpx.AsyncClient() as client:
        assert await anatomy.resolve_uberon(client, "notatissue") is None
        # negative cached → no second round-trip (case-different key normalizes the same)
        assert await anatomy.resolve_uberon(client, "NotATissue") is None
    assert len(httpx_mock.get_requests()) == 1


async def test_resolve_uberon_blank_name_returns_none_without_request(
    httpx_mock: HTTPXMock,
) -> None:
    async with httpx.AsyncClient() as client:
        assert await anatomy.resolve_uberon(client, "   ") is None
    assert httpx_mock.get_requests() == []


async def test_resolve_uberon_caches_positive_hit(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([_doc(label="liver", synonym=["iecur"])]))
    async with httpx.AsyncClient() as client:
        first = await anatomy.resolve_uberon(client, "liver")
        second = await anatomy.resolve_uberon(client, "Liver")  # case-different → same key
    assert first is not None and first.uberon_id == "UBERON:0002107"
    assert second is first
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_resolve_uberon_http_error_propagates_and_not_cached(monkeypatch) -> None:
    anatomy._CACHE.clear()
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        raise UpstreamUnavailableError("EBI OLS down")

    monkeypatch.setattr(anatomy._http, "request_json", boom)
    with pytest.raises(UpstreamUnavailableError):
        await anatomy.resolve_uberon(httpx.AsyncClient(), "liver")
    # not cached: a second call retries the lookup
    with pytest.raises(UpstreamUnavailableError):
        await anatomy.resolve_uberon(httpx.AsyncClient(), "liver")
    assert calls["n"] == 2


@live_only
async def test_live_resolve_uberon_liver() -> None:
    # Real-execution boundary check (contract from the plan): the canonical UBERON
    # term for "liver" is UBERON:0002107 / "liver" with synonyms incl. iecur/jecur.
    # If OLS surface forms drift, fix THIS assertion, not the code.
    anatomy._CACHE.clear()
    async with httpx.AsyncClient() as client:
        info = await anatomy.resolve_uberon(client, "liver")
        none = await anatomy.resolve_uberon(client, "notatissuexyz")
    assert info is not None
    assert info.uberon_id == "UBERON:0002107"
    assert info.canonical == "liver"
    syns = {s.lower() for s in info.synonyms}
    assert "iecur" in syns
    assert "jecur" in syns
    assert none is None
