"""Answers a real model gave ``query_understanding.rewrite``, replayed verbatim through the
real ``llm.complete_json`` parse."""

from __future__ import annotations

import json

import httpx

from data_aggregator_mcp import query_understanding

# llama3.1 (Ollama, temperature 0), 2026-10-02, for
# "arabidopsis root datasets between 2018 and 2016". Applied as-is, this range made a live
# zenodo+datacite search return 0 results with no error; the same years in order returned 10.
_LLAMA31_INVERTED_RANGE = {
    "keyword_core": "Arabidopsis root",
    "organism": "Arabidopsis",
    "disease": None,
    "tissue": "root",
    "chemical": None,
    "assay": None,
    "kind": "dataset",
    "year_min": 2018,
    "year_max": 2016,
    "confidence": 0.7,
}


async def _rewrite(monkeypatch, answer: dict) -> query_understanding.ParsedRewrite | None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await query_understanding.rewrite(client, "q")


async def test_an_inverted_year_range_is_put_in_order(monkeypatch) -> None:
    ru = await _rewrite(monkeypatch, _LLAMA31_INVERTED_RANGE)
    assert ru is not None
    assert (ru.year_min, ru.year_max) == (2016, 2018)
    assert ru.keyword_core == "Arabidopsis root"  # the rest of the answer still lands


async def test_an_ordered_or_one_sided_year_range_is_kept_as_given(monkeypatch) -> None:
    """Positive controls: only an inverted pair is reordered."""
    cases = {
        (2012, 2020): (2012, 2020),
        (2015, 2015): (2015, 2015),
        (2015, None): (2015, None),
        (None, 2014): (None, 2014),
    }
    for (lo, hi), want in cases.items():
        ru = await _rewrite(monkeypatch, {"keyword_core": "x", "year_min": lo, "year_max": hi})
        assert ru is not None
        assert (ru.year_min, ru.year_max) == want, (lo, hi)
