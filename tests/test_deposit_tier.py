"""Among title matches, data deposits come first.

Round 2 on 0.67.1 (2026-10-07): web search found the R4 figshare datasets S29, S30 and
S32 in every run, this server in one of four. Their titles name "snow leopard", but so
do the titles of every paper about snow leopards: DataCite ranked S29 and S32 120th and
114th of its 221 title matches, and the router's ranking tied them with the papers and
round-robined over ~20 sources, so the first figshare hit was 213th. Among DataCite's 69
dataset title matches they are 28th to 35th.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import _cursor, datacite, router, zenodo
from data_aggregator_mcp._relevance import title_clause
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
TITLE = 'titles.title:("snow leopard" OR "snow leopards")'
DEPOSITS = 'types.resourceTypeGeneral:("Collection" OR "Dataset")'


def _rec(i: int, title: str, kind: str, source: str = "datacite") -> DataResource:
    return DataResource(id=f"{source}:10.1/{i}", source=source, kind=kind, title=title)


def _upstream(held: list[DataResource], seen: list[str], *, deposit_clause: str | None):
    """An upstream that ranks by its own order and evaluates both clauses."""

    async def search(client, q, *, size=10, offset=0, plurals=True, filters=None):
        seen.append(q)
        pool = held
        if " AND NOT titles.title" in q:
            pool = [r for r in pool if "snow leopard" not in r.title.lower()]
        elif " AND titles.title" in q:
            pool = [r for r in pool if "snow leopard" in r.title.lower()]
        if deposit_clause and f" AND NOT {deposit_clause}" in q:
            pool = [r for r in pool if r.kind != "dataset"]
        elif deposit_clause and f" AND {deposit_clause}" in q:
            pool = [r for r in pool if r.kind == "dataset"]
        return len(pool), pool[offset : offset + size]

    attrs = {
        "search": search,
        "PREFIXES": frozenset(),
        "QUERY_PLURALS": True,
        "TITLE_FIELD": "titles.title",
    }
    if deposit_clause:
        attrs["DEPOSIT_CLAUSE"] = deposit_clause
    return types.SimpleNamespace(**attrs)


# 200 papers titled with the query, then 3 datasets titled with it, as DataCite ranked
# the snow leopard title matches; then 50 records naming it elsewhere.
HELD = (
    [_rec(i, f"Snow leopard ecology paper {i}", "publication") for i in range(200)]
    + [_rec(200 + i, f"Snow leopard detections {i}", "dataset") for i in range(3)]
    + [_rec(300 + i, f"Leopard tracks in snow {i}", "dataset") for i in range(50)]
)
DATASETS = [f"Snow leopard detections {i}" for i in range(3)]


async def _page(monkeypatch, adapter, **kw):
    monkeypatch.setattr(router, "_ADAPTERS", {"datacite": adapter})
    async with httpx.AsyncClient() as client:
        return await router.search_page(
            client, query="snow leopard", size=10, sources=["datacite"], **kw
        )


async def test_datasets_titled_with_the_query_lead_page_one(monkeypatch) -> None:
    seen: list[str] = []
    page = await _page(monkeypatch, _upstream(HELD, seen, deposit_clause=DEPOSITS))
    assert sorted(seen) == sorted(
        [
            f"(snow leopard) AND {TITLE} AND {DEPOSITS}",
            f"(snow leopard) AND {TITLE} AND NOT {DEPOSITS}",
            f"(snow leopard) AND NOT {TITLE}",
        ]
    )
    assert [r.title for r in page.results[:3]] == DATASETS
    # Split, not narrowed: the three totals add up to the source's own.
    assert page.total == len(HELD)
    # Then the papers titled with it, in the source's own order.
    assert [r.title for r in page.results[3:]] == [
        f"Snow leopard ecology paper {i}" for i in range(7)
    ]


async def test_a_source_without_a_deposit_clause_keeps_its_title_matches_whole(
    monkeypatch,
) -> None:
    """Control: the same upstream with no DEPOSIT_CLAUSE; its title matches are one
    stream in its own order, and the datasets 201st to 203rd stay off page one."""
    seen: list[str] = []
    page = await _page(monkeypatch, _upstream(HELD, seen, deposit_clause=None))
    assert sorted(seen) == sorted(
        [f"(snow leopard) AND {TITLE}", f"(snow leopard) AND NOT {TITLE}"]
    )
    assert not set(DATASETS) & {r.title for r in page.results}


async def test_a_cursor_from_before_the_deposit_tier_keeps_two_parts(monkeypatch) -> None:
    seen: list[str] = []
    adapter = _upstream(HELD, seen, deposit_clause=DEPOSITS)
    page = await _page(monkeypatch, adapter)
    state = _cursor.decode(page.next_cursor)
    assert state["dt"] is True
    # A cursor minted before has no "dt": its "datacite/title" offset indexes every
    # title match, papers and datasets together.
    state.pop("dt")
    state["offsets"] = {"datacite/title": 10, "datacite": 0}
    state["ahead"] = {}
    seen.clear()
    async with httpx.AsyncClient() as client:
        old = await router.search_page(client, cursor=_cursor.encode(state))
    assert sorted(seen) == sorted(
        [f"(snow leopard) AND {TITLE}", f"(snow leopard) AND NOT {TITLE}"]
    )
    assert [r.title for r in old.results] == [
        f"Snow leopard ecology paper {i}" for i in range(10, 20)
    ]


def _serves(records: list[DataResource]):
    async def search(client, q, *, size=10, offset=0, **kw):
        return len(records), records[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


async def test_a_deposit_ranks_before_a_paper_naming_the_search_as_well(monkeypatch) -> None:
    """Across sources: a paper source answers first in the round-robin, a data source
    second; with the match tied, the data comes first. Positive control: a dataset that
    names the query less (not in its title) stays behind the papers that do."""
    papers = [_rec(i, f"Snow leopard diet {i}", "publication", "openaire") for i in range(4)]
    data = [
        _rec(10, "Snow leopard scat genotypes", "dataset", "dryad"),
        _rec(11, "Leopard scat from snow fields", "dataset", "dryad"),
    ]
    monkeypatch.setattr(router, "_ADAPTERS", {"openaire": _serves(papers), "dryad": _serves(data)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(
            client, query="snow leopard", size=10, sources=["openaire", "dryad"]
        )
    assert [r.title for r in page.results] == [
        "Snow leopard scat genotypes",
        *(f"Snow leopard diet {i}" for i in range(4)),
        "Leopard scat from snow fields",
    ]


def test_the_deposit_clauses_select_the_dataset_kind_quoted() -> None:
    """Quoted, so the plural rewrite sends each type as written (an unquoted "Dataset"
    went upstream as "(Dataset OR Datasets)")."""
    assert datacite.DEPOSIT_CLAUSE == DEPOSITS
    assert zenodo.DEPOSIT_CLAUSE == 'resource_type.type:("dataset")'


_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
# The figshare datasets of R4 (benchmarks/h2h/round2/keys/R4.yaml), concept and version.
_FIGSHARE = {
    "S29": "10.6084/m9.figshare.19184897",
    "S30": "10.6084/m9.figshare.16691668",
    "S32": "10.6084/m9.figshare.25736751",
}


@_live
async def test_live_datacite_returns_the_figshare_snow_leopard_datasets_in_two_pages() -> None:
    """Probed 2026-10-07: DataCite's deposit part holds S29, S30 and S32 28th to 35th;
    its title part as one stream held S29 and S32 120th and 114th."""
    found: set[str] = set()
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(client, query="snow leopard", size=50, sources=["datacite"])
        for _ in range(2):
            found |= {(r.doi or "").casefold() for r in page.results}
            if not page.next_cursor:
                break
            page = await router.search_page(client, cursor=page.next_cursor)
    missing = {k for k, doi in _FIGSHARE.items() if not any(f.startswith(doi) for f in found)}
    assert not missing, missing


@_live
@pytest.mark.parametrize("adapter", [zenodo, datacite])
async def test_live_the_deposit_parts_split_the_title_matches(adapter) -> None:
    """The upstream reads the deposit clause on its type field: deposits plus the other
    title matches add up to the title matches, neither is empty, and every deposit is a
    dataset. A wrong field or type name matches nothing (or everything) and fails here."""
    clause = title_clause("snow leopard", adapter.TITLE_FIELD)
    deposits = adapter.DEPOSIT_CLAUSE
    async with httpx.AsyncClient(timeout=60) as client:
        titled, _ = await adapter.search(client, f"(snow leopard) AND {clause}", size=1)
        held, hits = await adapter.search(
            client, f"(snow leopard) AND {clause} AND {deposits}", size=25
        )
        other, _ = await adapter.search(
            client, f"(snow leopard) AND {clause} AND NOT {deposits}", size=1
        )
    assert 0 < held < titled and held + other == titled, (held, other, titled)
    assert hits and {r.kind for r in hits} == {"dataset"}, [(r.id, r.kind) for r in hits]
