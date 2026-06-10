from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import chemistry
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_OLS = "https://www.ebi.ac.uk/ols4/api/search"


def _doc(
    *,
    obo_id: str | None = "CHEBI:27732",
    label: str | None = "caffeine",
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


# --- _pick_chebi unit table --------------------------------------------------


def test_pick_chebi_exact_label_hit() -> None:
    docs = [_doc(label="caffeine", synonym=["1,3,7-trimethylxanthine"], is_defining_ontology=True)]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert info.chebi_id == "CHEBI:27732"
    assert info.canonical == "caffeine"
    assert info.synonyms == ("1,3,7-trimethylxanthine",)


def test_pick_chebi_synonym_exact_hit() -> None:
    # input matches a synonym, not the canonical label (case-insensitive).
    docs = [_doc(label="caffeine", synonym=["Theine"])]
    info = chemistry._pick_chebi(docs, "theine")
    assert info is not None
    assert info.canonical == "caffeine"
    assert info.chebi_id == "CHEBI:27732"


def test_pick_chebi_rejects_non_chebi_obo_id_even_if_label_matches() -> None:
    # cross-ontology leak guard: a non-CHEBI: obo_id must be rejected.
    docs = [_doc(obo_id="PR:000050567", label="caffeine", synonym=["theine"])]
    assert chemistry._pick_chebi(docs, "caffeine") is None


def test_pick_chebi_rejects_obsolete() -> None:
    docs = [_doc(label="caffeine", is_obsolete=True)]
    assert chemistry._pick_chebi(docs, "caffeine") is None


def test_pick_chebi_no_exact_match_returns_none() -> None:
    # top relevance hit is a related-but-different term → conservative None.
    docs = [_doc(obo_id="CHEBI:147464", label="aspirin-triggered protectin D1")]
    assert chemistry._pick_chebi(docs, "aspirin") is None


def test_pick_chebi_tolerates_absent_synonym_and_defining_fields() -> None:
    docs = [_doc(label="caffeine")]  # no synonym/is_defining_ontology/is_obsolete
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert info.canonical == "caffeine"
    assert info.synonyms == ()


def test_pick_chebi_prefers_defining_ontology_tiebreak() -> None:
    docs = [
        _doc(obo_id="CHEBI:00001", label="caffeine", is_defining_ontology=False),
        _doc(obo_id="CHEBI:27732", label="caffeine", is_defining_ontology=True),
    ]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert info.chebi_id == "CHEBI:27732"


def test_pick_chebi_drops_blank_synonyms() -> None:
    docs = [_doc(label="caffeine", synonym=["theine", "  ", 123, "guaranine"])]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert info.synonyms == ("theine", "guaranine")


def test_pick_chebi_scalar_synonym_not_char_exploded() -> None:
    # OLS (Solr) can return a single-valued `synonym` as a bare string. Iterating
    # it would explode "theine" into characters AND break the synonym match.
    docs = [{"obo_id": "CHEBI:27732", "label": "caffeine", "synonym": "theine"}]
    by_label = chemistry._pick_chebi(docs, "caffeine")
    assert by_label is not None
    assert by_label.synonyms == ("theine",)
    by_syn = chemistry._pick_chebi(docs, "theine")
    assert by_syn is not None
    assert by_syn.chebi_id == "CHEBI:27732"


def test_pick_chebi_empty_docs_returns_none() -> None:
    assert chemistry._pick_chebi([], "caffeine") is None


def test_pick_chebi_caps_synonyms_to_max() -> None:
    # ChEBI realism: large synonym lists are capped to _MAX_SYNONYMS (canonical kept).
    many = [f"syn{i}" for i in range(20)]
    docs = [_doc(label="caffeine", synonym=many)]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert len(info.synonyms) == chemistry._MAX_SYNONYMS == 12
    # the cap keeps the FIRST 12 in order.
    assert info.synonyms == tuple(many[:12])


def test_pick_chebi_under_cap_unaffected() -> None:
    few = [f"syn{i}" for i in range(5)]
    docs = [_doc(label="caffeine", synonym=few)]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    assert info.synonyms == tuple(few)


def test_pick_chebi_case_dedups_and_drops_canonical_before_cap() -> None:
    # ChEBI emits case/format variants + the canonical label as synonyms; the cap
    # budget must hold DISTINCT terms (case-insensitive), excluding the canonical.
    docs = [
        _doc(
            label="caffeine",
            synonym=["CAFFEINE", "Caffeine", "caffeine", "theine", "Theine", "guaranine"],
        )
    ]
    info = chemistry._pick_chebi(docs, "caffeine")
    assert info is not None
    # "CAFFEINE"/"Caffeine"/"caffeine" all collapse to the canonical (dropped);
    # "theine"/"Theine" collapse to one; "guaranine" stays. Order preserved.
    assert info.synonyms == ("theine", "guaranine")


# --- resolve_chebi -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_chemistry_cache():
    chemistry._CACHE.clear()
    yield
    chemistry._CACHE.clear()


async def test_resolve_chebi_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_OLS
        + "?q=caffeine&ontology=chebi&exact=true"
        + "&fieldList=obo_id%2Clabel%2Csynonym%2Cis_defining_ontology%2Cis_obsolete&rows=10",
        json=_envelope([_doc(label="caffeine", synonym=["theine", "guaranine"])]),
    )
    async with httpx.AsyncClient() as client:
        info = await chemistry.resolve_chebi(client, "caffeine")
    assert info is not None
    assert info.chebi_id == "CHEBI:27732"
    assert info.canonical == "caffeine"
    assert info.synonyms == ("theine", "guaranine")


async def test_resolve_chebi_no_match_returns_none_and_caches(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([]))
    async with httpx.AsyncClient() as client:
        assert await chemistry.resolve_chebi(client, "notachemical") is None
        # negative cached → no second round-trip (case-different key normalizes the same)
        assert await chemistry.resolve_chebi(client, "NotAChemical") is None
    assert len(httpx_mock.get_requests()) == 1


async def test_resolve_chebi_blank_name_returns_none_without_request(
    httpx_mock: HTTPXMock,
) -> None:
    async with httpx.AsyncClient() as client:
        assert await chemistry.resolve_chebi(client, "   ") is None
    assert httpx_mock.get_requests() == []


async def test_resolve_chebi_caches_positive_hit(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_envelope([_doc(label="caffeine", synonym=["theine"])]))
    async with httpx.AsyncClient() as client:
        first = await chemistry.resolve_chebi(client, "caffeine")
        second = await chemistry.resolve_chebi(client, "Caffeine")  # case-different → same key
    assert first is not None and first.chebi_id == "CHEBI:27732"
    assert second is first
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_resolve_chebi_http_error_propagates_and_not_cached(monkeypatch) -> None:
    chemistry._CACHE.clear()
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        raise UpstreamUnavailableError("EBI OLS down")

    monkeypatch.setattr(chemistry._http, "request_json", boom)
    with pytest.raises(UpstreamUnavailableError):
        await chemistry.resolve_chebi(httpx.AsyncClient(), "caffeine")
    with pytest.raises(UpstreamUnavailableError):
        await chemistry.resolve_chebi(httpx.AsyncClient(), "caffeine")
    assert calls["n"] == 2


@live_only
async def test_live_resolve_chebi_caffeine() -> None:
    # Real-execution boundary check (contract from the plan): caffeine → CHEBI:27732,
    # synonyms non-empty and capped <= 12. junk → None. If OLS surface forms drift,
    # fix THIS assertion, not the code.
    chemistry._CACHE.clear()
    async with httpx.AsyncClient() as client:
        info = await chemistry.resolve_chebi(client, "caffeine")
        none = await chemistry.resolve_chebi(client, "zzzznotachemical")
    assert info is not None
    assert info.chebi_id == "CHEBI:27732"
    assert info.canonical == "caffeine"
    assert len(info.synonyms) > 0
    assert len(info.synonyms) <= chemistry._MAX_SYNONYMS
    assert none is None
