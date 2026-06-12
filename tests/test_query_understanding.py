from __future__ import annotations

from unittest.mock import AsyncMock

import httpx

from data_aggregator_mcp import llm, query_understanding


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")


async def test_rewrite_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    called = AsyncMock()
    monkeypatch.setattr(llm, "complete_json", called)
    async with httpx.AsyncClient() as client:
        assert await query_understanding.rewrite(client, "q") is None
    called.assert_not_awaited()  # never even calls the LLM when disabled


async def test_rewrite_maps_fields_and_coerces_years(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(
            return_value={
                "keyword_core": "rna degradation",
                "organism": "Zea mays",
                "disease": None,
                "tissue": "leaf",
                "chemical": "",
                "assay": "RNA-seq",
                "kind": "dataset",
                "year_min": "2015",
                "year_max": 2020.0,
            }
        ),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "maize rna decay in leaves since 2015")
    assert ru is not None
    assert ru.keyword_core == "rna degradation"
    assert ru.organism == "Zea mays"
    assert ru.disease is None
    assert ru.tissue == "leaf"
    assert ru.chemical is None  # empty string → None
    assert ru.assay == "RNA-seq"
    assert ru.kind == "dataset"
    assert ru.year_min == 2015  # str coerced
    assert ru.year_max == 2020  # float coerced


async def test_rewrite_drops_invalid_kind(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"keyword_core": "x", "kind": "spreadsheet"}),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "x")
    assert ru is not None
    assert ru.kind is None  # invalid kind dropped, not passed downstream


async def test_rewrite_none_when_llm_returns_none(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(llm, "complete_json", AsyncMock(return_value=None))
    async with httpx.AsyncClient() as client:
        assert await query_understanding.rewrite(client, "x") is None


async def test_rewrite_none_when_nothing_usable(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(
            return_value={
                "keyword_core": None,
                "organism": "",
                "kind": "not-a-kind",
                "year_min": "abc",
            }
        ),
    )
    async with httpx.AsyncClient() as client:
        assert await query_understanding.rewrite(client, "x") is None


async def test_rewrite_never_raises_on_garbage_types(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(
            return_value={
                "keyword_core": ["a", "list"],  # wrong type
                "year_min": {"nested": 1},  # wrong type
                "organism": "Homo sapiens",
            }
        ),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "x")
    assert ru is not None
    assert ru.keyword_core is None  # non-str dropped
    assert ru.year_min is None  # non-coercible dropped
    assert ru.organism == "Homo sapiens"


async def test_rewrite_captures_confidence(monkeypatch) -> None:
    # E2: advisory, uncalibrated self-confidence rides along with a real rewrite.
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"keyword_core": "liver scRNA", "confidence": 0.8}),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "single cell liver")
    assert ru is not None
    assert ru.confidence == 0.8


async def test_rewrite_confidence_clamped_to_unit(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"keyword_core": "x", "confidence": 1.5}),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "x")
    assert ru is not None
    assert ru.confidence == 1.0


async def test_rewrite_confidence_invalid_is_none(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"keyword_core": "x", "confidence": "high"}),
    )
    async with httpx.AsyncClient() as client:
        ru = await query_understanding.rewrite(client, "x")
    assert ru is not None
    assert ru.confidence is None


async def test_rewrite_confidence_alone_is_not_usable(monkeypatch) -> None:
    # A confidence with no actual rewrite content must stay a no-op rewrite (None).
    _enable(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"confidence": 0.9}),
    )
    async with httpx.AsyncClient() as client:
        assert await query_understanding.rewrite(client, "x") is None
