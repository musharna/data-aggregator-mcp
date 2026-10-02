"""Observed behaviour of ``query_understanding``: the exact request each call sends to the
LLM endpoint, and the exact mapping of each answer field. Driven through the real
``llm.complete_json`` over an ``httpx.MockTransport``, so the client, the prompt and the
query are what actually leave the process, not what a mock was handed."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from data_aggregator_mcp import query_understanding, router

_BASE = "https://llm.test/v1"


def _llm(monkeypatch, answer: dict, seen: list[dict]) -> httpx.AsyncClient:
    """A client whose transport plays an OpenAI-compatible endpoint answering ``answer``,
    recording each request body in ``seen``."""
    monkeypatch.setenv("LLM_API_BASE", _BASE)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert str(req.url) == f"{_BASE}/chat/completions"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _messages(body: dict) -> tuple[str, str]:
    (system, user) = body["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    return system["content"], user["content"]


# ---------------------------------------------------------------------------
# rewrite
# ---------------------------------------------------------------------------


async def test_rewrite_sends_the_structured_prompt_and_the_query(monkeypatch) -> None:
    seen: list[dict] = []
    async with _llm(monkeypatch, {"keyword_core": "liver scRNA-seq"}, seen) as client:
        ru = await query_understanding.rewrite(client, "single-cell RNA-seq of human liver")
    assert ru is not None and ru.keyword_core == "liver scRNA-seq"
    assert len(seen) == 1
    system, user = _messages(seen[0])
    assert system == query_understanding._SYSTEM_PROMPT
    assert user == "single-cell RNA-seq of human liver"


async def test_rewrite_maps_every_entity_field_to_its_own_key(monkeypatch) -> None:
    """Each advisory facet is read from the key of the same name, and only that key: a
    distinct value per key, so a swapped or misspelled key cannot land the right value."""
    answer = {
        "keyword_core": "core",
        "organism": "Homo sapiens",
        "disease": "hepatitis B",
        "tissue": "liver",
        "chemical": "acetaminophen",
        "assay": "RNA-seq",
        "kind": "study",
        "year_min": 2010,
        "year_max": 2020,
        "confidence": 0.25,
    }
    async with _llm(monkeypatch, answer, []) as client:
        ru = await query_understanding.rewrite(client, "q")
    assert ru == query_understanding.ParsedRewrite(**answer)


async def test_rewrite_strips_and_drops_blank_entity_fields(monkeypatch) -> None:
    answer = {"keyword_core": "x", "disease": "  malaria ", "chemical": "   "}
    async with _llm(monkeypatch, answer, []) as client:
        ru = await query_understanding.rewrite(client, "q")
    assert ru is not None
    assert ru.disease == "malaria"
    assert ru.chemical is None


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


async def test_expand_sends_the_variant_prompt_and_the_query(monkeypatch) -> None:
    seen: list[dict] = []
    async with _llm(monkeypatch, {"variants": ["maize transcriptome"]}, seen) as client:
        out = await query_understanding.expand(client, "maize rna", n=4)
    assert out == ["maize transcriptome"]
    assert len(seen) == 1
    system, user = _messages(seen[0])
    assert system == query_understanding._EXPAND_SYSTEM_PROMPT.format(n=3)
    assert system.startswith("Generate up to 3 ALTERNATIVE search queries")
    assert user == "maize rna"


async def test_expand_asks_for_n_minus_one_variants_but_never_fewer_than_one(
    monkeypatch,
) -> None:
    """The caller prepends the original as variant 0, so the LLM is asked for ``n - 1``;
    for ``n`` of 1 or 2 that is one alternative, never zero or a negative count."""
    asked: dict[int, str] = {}
    for n in (1, 2, 3, 5):
        seen: list[dict] = []
        async with _llm(monkeypatch, {"variants": ["alt"]}, seen) as client:
            assert await query_understanding.expand(client, "q", n=n) == ["alt"]
        asked[n] = _messages(seen[0])[0].split(" ALTERNATIVE")[0]
    assert asked == {
        1: "Generate up to 1",
        2: "Generate up to 1",
        3: "Generate up to 2",
        5: "Generate up to 4",
    }


# ---------------------------------------------------------------------------
# live: a real OpenAI-compatible endpoint (LLM_API_BASE), read at collection, before the
# autouse isolation clears it; ``live_env`` restores it for the test body.
# ---------------------------------------------------------------------------

_live_llm = pytest.mark.skipif(
    not (os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1" and os.environ.get("LLM_API_BASE")),
    reason="set DATA_AGGREGATOR_MCP_LIVE=1 and LLM_API_BASE to run",
)


@_live_llm
async def test_live_understand_yields_wellformed_echo(live_env) -> None:
    """A real LLM endpoint rewrites a natural-language query end to end, and the search
    returns the echo and results. (Moved from test_router.py, where it read LLM_API_BASE
    after the test isolation had cleared it, so it skipped on every run.)"""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await router.search_page(
            client,
            query="single-cell RNA sequencing datasets of human liver",
            size=10,
            understand=True,
        )
    assert "understand" not in result.errors
    qu = result.query_understanding
    assert qu is not None
    assert qu.input == "single-cell RNA sequencing datasets of human liver"
    assert qu.keyword_core and qu.applied["keyword_core"] == qu.keyword_core
    assert result.results


@_live_llm
async def test_live_years_stated_high_to_low_are_searched_in_order(live_env) -> None:
    """llama3.1 answers this query with year_min=2018, year_max=2016; applied as-is, the
    search found nothing and reported no error. Positive control: records are found."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await router.search_page(
            client,
            query="arabidopsis root datasets between 2018 and 2016",
            size=10,
            sources=["zenodo", "datacite"],
            understand=True,
        )
    assert result.errors == {}
    qu = result.query_understanding
    assert qu is not None
    assert (qu.applied["year_min"], qu.applied["year_max"]) == (2016, 2018)
    assert result.results
    assert all(r.year is not None and 2016 <= r.year <= 2018 for r in result.results)


@_live_llm
async def test_live_expand_returns_alternative_queries(live_env) -> None:
    async with httpx.AsyncClient() as client:
        out = await query_understanding.expand(client, "maize drought RNA-seq", n=4)
    assert out, "the endpoint answered no usable variants"
    assert all(isinstance(v, str) and v.strip() == v and v for v in out)
