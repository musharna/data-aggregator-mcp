"""A page says how many hits it already fetched, did not send, and name the query.

R4 re-runs (2026-10-06): the agents listed nearly every snow leopard study they were
shown and almost never asked for a second page, though six pages of "snow leopard" held
27 of the 35 keyed studies and the first held 16. Nothing on a page told them the next
one was still on topic: ``total`` (48,435) counts every loose upstream match.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import router, server
from data_aggregator_mcp._relevance import query_terms
from data_aggregator_mcp.models import DataResource, compact

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


def _adapter(recs: list[DataResource]):
    async def search(client, q, *, size=10, offset=0):
        return len(recs), [compact(r) for r in recs[offset : offset + size]]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


def _rec(source: str, i: int, title: str, kind: str = "dataset") -> DataResource:
    return DataResource(id=f"{source}:{i}", source=source, kind=kind, title=title)


async def _page(monkeypatch, held: dict[str, list[DataResource]], **kw):
    monkeypatch.setattr(router, "_ADAPTERS", {s: _adapter(r) for s, r in held.items()})
    async with httpx.AsyncClient() as client:
        first = await router.search_page(
            client, query="snow leopard", size=10, sources=list(held), **kw
        )
        second = (
            await router.search_page(client, cursor=first.next_cursor)
            if first.next_cursor
            else None
        )
    return first, second


@pytest.mark.parametrize("left", [1, 4])
async def test_a_page_counts_the_fetched_hits_naming_the_query_that_come_next(
    monkeypatch, left: int
) -> None:
    held = {
        "zenodo": [_rec("zenodo", i, f"Snow leopard survey {i}") for i in range(10)],
        "dataone": [_rec("dataone", i, f"Snow leopards, scat {i}") for i in range(left)]
        + [_rec("dataone", i, f"Arctic fox {i}") for i in range(left, 10)],
    }
    first, second = await _page(monkeypatch, held)

    assert first.count == 10 and first.next_cursor
    assert first.errors["next_page"] == (
        f"{left} more hits already fetched name every word of the query; "
        "next_cursor continues with them"
    )
    # The note is true: the next page leads with those hits.
    terms = query_terms("snow leopard")
    lead = [r.title.lower() for r in second.results[:left]]
    assert all(all(t in title for t in terms) for title in lead), lead


async def test_no_note_when_what_is_left_does_not_name_the_query(monkeypatch) -> None:
    """Control: the same page, with everything left over naming something else."""
    held = {
        "zenodo": [_rec("zenodo", i, f"Snow leopard survey {i}") for i in range(10)],
        "dataone": [_rec("dataone", i, f"Arctic fox {i}") for i in range(10)],
    }
    first, _ = await _page(monkeypatch, held)
    assert first.count == 10 and first.next_cursor
    assert "next_page" not in first.errors


async def test_a_hit_the_filters_would_drop_is_not_counted(monkeypatch) -> None:
    held = {
        "zenodo": [_rec("zenodo", i, f"Snow leopard survey {i}") for i in range(10)],
        "dataone": [_rec("dataone", i, f"Snow leopard data {i}") for i in range(7)]
        + [_rec("dataone", i, f"Snow leopard code {i}", "software") for i in range(7, 10)],
    }
    first, _ = await _page(monkeypatch, held, kind="dataset")
    assert first.count == 10
    # 17 datasets, 10 sent: 7 remain. The 3 software hits rank last, so the page stops
    # before reaching them; they wait too, but the filters would drop them.
    assert first.errors["next_page"].startswith("7 more hits already fetched")


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_a_snow_leopard_page_says_more_name_it() -> None:
    first = await server._dispatch("search", {"query": "snow leopard", "size": 20})
    note = first["errors"]["next_page"]
    assert int(note.split()[0]) >= 1, note
    second = await server._dispatch("search", {"cursor": first["next_cursor"]})
    titles = [h["title"].lower() for h in second["results"][:5]]
    assert all("snow" in t and "leopard" in t for t in titles), titles
