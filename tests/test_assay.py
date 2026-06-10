from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import assay
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_OLS = "https://www.ebi.ac.uk/ols4/api/search"


def _doc(
    *,
    obo_id: str | None = "EDAM:topic_3169",
    label: str | None = "ChIP-seq",
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


# --- _pick_edam unit table ---------------------------------------------------


def test_pick_edam_exact_label_hit() -> None:
    docs = [_doc(label="ChIP-seq", synonym=["ChIP-exo", "ChIP-sequencing"])]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.edam_id == "EDAM:topic_3169"
    assert info.canonical == "ChIP-seq"
    assert info.synonyms == ("ChIP-exo", "ChIP-sequencing")


def test_pick_edam_synonym_exact_hit() -> None:
    docs = [_doc(label="ChIP-seq", synonym=["ChIP-sequencing"])]
    info = assay._pick_edam(docs, "chip-sequencing")
    assert info is not None
    assert info.canonical == "ChIP-seq"
    assert info.edam_id == "EDAM:topic_3169"


def test_pick_edam_rejects_non_topic_data_id() -> None:
    # EDAM mixes id-classes; only topic_ is an assay/method concept.
    docs = [_doc(obo_id="EDAM:data_3917", label="ChIP-seq")]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_rejects_non_topic_format_id() -> None:
    docs = [_doc(obo_id="EDAM:format_2333", label="ChIP-seq")]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_rejects_non_topic_operation_id() -> None:
    docs = [_doc(obo_id="EDAM:operation_3204", label="ChIP-seq")]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_rejects_cross_ontology_obo_id() -> None:
    docs = [_doc(obo_id="OBI:0000716", label="ChIP-seq")]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_accepts_only_topic_prefix() -> None:
    # the right id-class is accepted.
    docs = [_doc(obo_id="EDAM:topic_3169", label="ChIP-seq")]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.edam_id == "EDAM:topic_3169"


def test_pick_edam_rejects_obsolete() -> None:
    docs = [_doc(label="ChIP-seq", is_obsolete=True)]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_no_exact_match_returns_none() -> None:
    docs = [_doc(obo_id="EDAM:topic_3170", label="RNA-Seq")]
    assert assay._pick_edam(docs, "chip-seq") is None


def test_pick_edam_tolerates_absent_synonym_and_defining_fields() -> None:
    docs = [_doc(label="ChIP-seq")]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.canonical == "ChIP-seq"
    assert info.synonyms == ()


def test_pick_edam_prefers_defining_ontology_tiebreak() -> None:
    docs = [
        _doc(obo_id="EDAM:topic_0001", label="ChIP-seq", is_defining_ontology=False),
        _doc(obo_id="EDAM:topic_3169", label="ChIP-seq", is_defining_ontology=True),
    ]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.edam_id == "EDAM:topic_3169"


def test_pick_edam_drops_blank_synonyms() -> None:
    docs = [_doc(label="ChIP-seq", synonym=["ChIP-exo", "  ", 123, "ChIP-sequencing"])]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.synonyms == ("ChIP-exo", "ChIP-sequencing")


def test_pick_edam_scalar_synonym_not_char_exploded() -> None:
    docs = [{"obo_id": "EDAM:topic_3169", "label": "ChIP-seq", "synonym": "ChIP-exo"}]
    by_label = assay._pick_edam(docs, "chip-seq")
    assert by_label is not None
    assert by_label.synonyms == ("ChIP-exo",)
    by_syn = assay._pick_edam(docs, "chip-exo")
    assert by_syn is not None
    assert by_syn.edam_id == "EDAM:topic_3169"


def test_pick_edam_no_synonym_cap() -> None:
    # EDAM lists are small; no cap applies (parity check vs ChEBI's cap).
    many = [f"syn{i}" for i in range(20)]
    docs = [_doc(label="ChIP-seq", synonym=many)]
    info = assay._pick_edam(docs, "chip-seq")
    assert info is not None
    assert info.synonyms == tuple(many)


def test_pick_edam_empty_docs_returns_none() -> None:
    assert assay._pick_edam([], "chip-seq") is None


# --- resolve_edam ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_assay_cache():
    assay._CACHE.clear()
    yield
    assay._CACHE.clear()


async def test_resolve_edam_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_OLS
        + "?q=ChIP-seq&ontology=edam&exact=true"
        + "&fieldList=obo_id%2Clabel%2Csynonym%2Cis_defining_ontology%2Cis_obsolete&rows=10",
        json=_envelope([_doc(label="ChIP-seq", synonym=["ChIP-exo", "ChIP-sequencing"])]),
    )
    async with httpx.AsyncClient() as client:
        info = await assay.resolve_edam(client, "ChIP-seq")
    assert info is not None
    assert info.edam_id == "EDAM:topic_3169"
    assert info.canonical == "ChIP-seq"
    assert info.synonyms == ("ChIP-exo", "ChIP-sequencing")


async def test_resolve_edam_no_match_returns_none_and_caches(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([]))
    async with httpx.AsyncClient() as client:
        assert await assay.resolve_edam(client, "notanassay") is None
        assert await assay.resolve_edam(client, "NotAnAssay") is None
    assert len(httpx_mock.get_requests()) == 1


async def test_resolve_edam_blank_name_returns_none_without_request(
    httpx_mock: HTTPXMock,
) -> None:
    async with httpx.AsyncClient() as client:
        assert await assay.resolve_edam(client, "   ") is None
    assert httpx_mock.get_requests() == []


async def test_resolve_edam_caches_positive_hit(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([_doc(label="ChIP-seq", synonym=["ChIP-exo"])]))
    async with httpx.AsyncClient() as client:
        first = await assay.resolve_edam(client, "ChIP-seq")
        second = await assay.resolve_edam(client, "chip-seq")  # case-different → same key
    assert first is not None and first.edam_id == "EDAM:topic_3169"
    assert second is first
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_resolve_edam_http_error_propagates_and_not_cached(monkeypatch) -> None:
    assay._CACHE.clear()
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        raise UpstreamUnavailableError("EBI OLS down")

    monkeypatch.setattr(assay._http, "request_json", boom)
    with pytest.raises(UpstreamUnavailableError):
        await assay.resolve_edam(httpx.AsyncClient(), "ChIP-seq")
    with pytest.raises(UpstreamUnavailableError):
        await assay.resolve_edam(httpx.AsyncClient(), "ChIP-seq")
    assert calls["n"] == 2


@live_only
async def test_live_resolve_edam_chipseq() -> None:
    # Real-execution boundary check (contract from the plan): ChIP-seq → EDAM:topic_3169,
    # canonical "ChIP-seq", synonyms include a ChIP-seq variant. junk → None. If OLS
    # surface forms drift, fix THIS assertion, not the code.
    assay._CACHE.clear()
    async with httpx.AsyncClient() as client:
        info = await assay.resolve_edam(client, "ChIP-seq")
        none = await assay.resolve_edam(client, "zzzznotanassay")
    assert info is not None
    assert info.edam_id == "EDAM:topic_3169"
    assert info.canonical == "ChIP-seq"
    syns = {s.lower() for s in info.synonyms}
    assert any("chip-seq" in s or "chip-sequencing" in s for s in syns)
    assert none is None
