"""Sources that can search a title field are asked for title matches first.

R4 re-runs (2026-10-06): the ranking puts a hit naming the query in its title first, but
only among the hits it fetched, and DataCite and Zenodo rank by their own relevance.
DataCite ranked "Species presence data - snow leopard, siberian ibex..." 158th of 480
for "snow leopard", Zenodo did not have it in its first 125 of 12,039, and no six-page
search reached it. Asked for the title tier, DataCite has it 43rd of 221 and Zenodo
51st of 56.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import _cursor, router
from data_aggregator_mcp._relevance import _names, _normalize, title_clause
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
CLAUSE = 'titles.title:("snow leopard" OR "snow leopards")'


def test_title_clause_for_plain_words() -> None:
    assert title_clause("snow leopard", "titles.title") == CLAUSE
    assert title_clause("  snow   leopard ", "title") == 'title:("snow leopard" OR "snow leopards")'
    # A last word already plural, or too short to pluralise, is sent as written.
    assert title_clause("snow leopards", "title") == 'title:"snow leopards"'
    assert (
        title_clause("Panthera uncia", "title") == 'title:("Panthera uncia" OR "Panthera uncias")'
    )
    for query in (
        '"snow leopard"',
        "snow AND leopard",
        "snow OR leopard",
        "title:snow",
        "leop*",
        "(snow leopard)",
        "snow-leopard!",
        "snow -leopard",  # Lucene reads a leading "-" as NOT
        "",
        "   ",
    ):
        assert title_clause(query, "title") is None, query


def _rec(i: int, title: str) -> DataResource:
    return DataResource(id=f"datacite:10.1/{i}", source="datacite", kind="dataset", title=title)


def _upstream(held: list[DataResource], seen: list[str], *, title_field: str | None):
    """An upstream that ranks by its own order and evaluates the title clause."""

    async def search(client, q, *, size=10, offset=0, plurals=True):
        seen.append(q)
        if " AND NOT " in q:
            pool = [r for r in held if "snow leopard" not in r.title.lower()]
        elif " AND " in q:
            pool = [r for r in held if "snow leopard" in r.title.lower()]
        else:
            pool = held
        return len(pool), pool[offset : offset + size]

    attrs = {"search": search, "PREFIXES": frozenset(), "QUERY_PLURALS": True}
    if title_field:
        attrs["TITLE_FIELD"] = title_field
    return types.SimpleNamespace(**attrs)


# 200 hits naming both words apart, then 3 naming the query in the title, as DataCite
# ranked the snow leopard deposits.
HELD = [_rec(i, f"Leopard tracks in snow {i}") for i in range(200)] + [
    _rec(200 + i, f"Snow leopard occurrences {i}") for i in range(3)
]


async def _first_page(monkeypatch, adapter, **kw):
    monkeypatch.setattr(router, "_ADAPTERS", {"datacite": adapter})
    async with httpx.AsyncClient() as client:
        return await router.search_page(
            client, query="snow leopard", size=10, sources=["datacite"], **kw
        )


async def test_title_matches_a_source_ranks_deep_come_on_page_one(monkeypatch) -> None:
    seen: list[str] = []
    page = await _first_page(monkeypatch, _upstream(HELD, seen, title_field="titles.title"))
    assert sorted(seen) == sorted(
        [f"(snow leopard) AND {CLAUSE}", f"(snow leopard) AND NOT {CLAUSE}"]
    )
    assert [r.title for r in page.results[:3]] == [
        f"Snow leopard occurrences {i}" for i in range(3)
    ]
    # Split, not narrowed: the two totals add up to the source's own.
    assert page.total == len(HELD)
    # The rest of the page is the source's own order.
    assert [r.title for r in page.results[3:]] == [f"Leopard tracks in snow {i}" for i in range(7)]


async def test_a_source_without_a_title_field_is_sent_the_query_alone(monkeypatch) -> None:
    """Control: no TITLE_FIELD, one query, the source's order, so the deep hits stay deep."""
    seen: list[str] = []
    page = await _first_page(monkeypatch, _upstream(HELD, seen, title_field=None))
    assert seen == ["snow leopard"]
    assert not any("Snow leopard occurrences" in r.title for r in page.results)


async def test_a_query_that_is_not_plain_words_is_sent_alone(monkeypatch) -> None:
    seen: list[str] = []
    adapter = _upstream(HELD, seen, title_field="titles.title")
    monkeypatch.setattr(router, "_ADAPTERS", {"datacite": adapter})
    async with httpx.AsyncClient() as client:
        await router.search_page(client, query='"snow leopard"', size=10, sources=["datacite"])
    assert seen == ['"snow leopard"']


async def test_a_cursor_from_before_the_title_tier_keeps_one_query(monkeypatch) -> None:
    seen: list[str] = []
    page = await _first_page(monkeypatch, _upstream(HELD, seen, title_field="titles.title"))
    state = _cursor.decode(page.next_cursor)
    assert state["tt"] is True
    # A cursor minted before has no "tt": its offsets index the query as one stream.
    state.pop("tt")
    state["offsets"] = {"datacite": 10}
    state["ahead"] = {}
    seen.clear()
    async with httpx.AsyncClient() as client:
        old = await router.search_page(client, cursor=_cursor.encode(state))
    assert seen == ["snow leopard"]
    assert [r.title for r in old.results] == [f"Leopard tracks in snow {i}" for i in range(10, 20)]


_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
# Every id of S27 and S28 in benchmarks/h2h/round2/keys/R4.yaml (concepts and versions).
_S27 = {"zenodo:20160628", "zenodo:20160629"}
_S28 = {"zenodo:22252314", "zenodo:22252315", "zenodo:22253310", "zenodo:22253311"}


@_live
async def test_live_zenodo_reaches_the_buried_snow_leopard_deposits_in_three_pages() -> None:
    """Probed 2026-10-06: with the title tier, page 3 holds S27 and S28; without it,
    neither is in the first 8 pages (200 hits)."""
    found: set[str] = set()
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(client, query="snow leopard", size=50, sources=["zenodo"])
        for _ in range(3):
            found |= {r.id for r in page.results}
            if not page.next_cursor:
                break
            page = await router.search_page(client, cursor=page.next_cursor)
    assert _S27 & found and _S28 & found, sorted(found)[:20]


@_live
@pytest.mark.parametrize("source", ["zenodo", "datacite"])
async def test_live_the_two_halves_split_the_source_total(source: str) -> None:
    """The upstream reads the clause on its title field: the halves add up to the whole,
    neither is empty, and the title half's hits name the query in their title. A wrong
    field name matches nothing (or everything) and fails here, not silently."""
    adapter = router._ADAPTERS[source]
    clause = title_clause("snow leopard", adapter.TITLE_FIELD)
    async with httpx.AsyncClient(timeout=60) as client:
        whole, _ = await adapter.search(client, "snow leopard", size=1)
        titled, hits = await adapter.search(client, f"(snow leopard) AND {clause}", size=25)
        rest, _ = await adapter.search(client, f"(snow leopard) AND NOT {clause}", size=1)
    assert 0 < titled < whole and titled + rest == whole, (titled, rest, whole)
    assert hits and all(_names(_normalize(r.title), "snow leopard") for r in hits), [
        r.title for r in hits
    ]
