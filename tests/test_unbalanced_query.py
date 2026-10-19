"""A query with an unmatched quote or parenthesis is repaired before it is sent.

Probed 2026-10-17 on every source with `"snow leopard` and `liver (proteome`: the quote
broke 8 sources (DataCite, DANDI, OpenAIRE, UniProt and NASA CMR HTTP 400, OmicsDI 404,
OpenML 412, NGDC an unparseable body three times) and the parenthesis 5, each reported
as an outage, while Zenodo, PubMed, NCBI and GBIF answered. The mark is dropped once, in
the router, and the page says what was searched.
"""

from __future__ import annotations

import types

import httpx
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp._relevance import balance_query
from data_aggregator_mcp.models import DataResource


@pytest.mark.parametrize(
    ("query", "repaired", "dropped"),
    [
        ('"snow leopard', "snow leopard", ['"']),
        ('snow leopard"', "snow leopard", ['"']),
        ('"a b" "c', '"a b" c', ['"']),
        ("liver (proteome", "liver proteome", ["("]),
        ("liver proteome)", "liver proteome", [")"]),
        ("(a OR (b) c", "a OR (b) c", ["("]),
        ('a ")" (b', 'a ")" b', ["("]),  # a parenthesis inside quotes is text, not syntax
    ],
)
def test_an_unmatched_mark_is_dropped_and_named(query, repaired, dropped) -> None:
    assert balance_query(query) == (repaired, dropped)


@pytest.mark.parametrize(
    "query",
    [
        '"snow leopard"',
        "(tendon OR ligament) human",
        'a \\" b',
        'title "(1) a review"',  # a parenthesis inside quotes is text
        "it's",
        "plain words",
        "",
    ],
)
def test_a_balanced_query_is_sent_as_written(query) -> None:
    assert balance_query(query) == (query, [])


def _rec(i: int) -> DataResource:
    return DataResource(id=f"dataone:{i}", source="dataone", kind="dataset", title=f"r {i}")


async def test_the_router_sends_the_repaired_query_and_says_so(monkeypatch) -> None:
    seen: list[str] = []
    recs = [_rec(i) for i in range(4)]

    async def search(client, q, *, size=10, offset=0):
        seen.append(q)
        return len(recs), recs[offset : offset + size]

    adapter = types.SimpleNamespace(search=search, PREFIXES=frozenset())
    monkeypatch.setattr(router, "_ADAPTERS", {"dataone": adapter})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query='"snow leopard', size=2)
        more = await router.search_page(client, cursor=page.next_cursor)
        plain = await router.search_page(client, query='"snow leopard"', size=2)
    assert seen == ["snow leopard", "snow leopard", '"snow leopard"']
    assert page.errors["unbalanced"] == (
        'dropped an unmatched " that most sources cannot parse; searched for: snow leopard'
    )
    assert [r.id for r in page.results + more.results] == [r.id for r in recs]
    assert "unbalanced" not in plain.errors  # positive control: a balanced query, no note
    async with httpx.AsyncClient() as client:
        two = await router.search_page(client, query='"snow (leopard', size=2)
    assert two.errors["unbalanced"] == (
        'dropped an unmatched " ( that most sources cannot parse; searched for: snow leopard'
    )


async def test_a_multi_query_variant_is_repaired_too(monkeypatch) -> None:
    """The LLM writes the variants; one with an unmatched quote broke the same sources."""
    from unittest.mock import AsyncMock

    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setattr(
        router.query_understanding_mod, "expand", AsyncMock(return_value=['alt "query'])
    )
    seen: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0, **_):
        seen.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        await router.search_page(client, query="orig", sources=["zenodo"], multi_query=True)
    assert any("alt query" in q for q in seen), seen  # positive control: the variant ran
    assert [q for q in seen if q.count('"') % 2] == [], seen
