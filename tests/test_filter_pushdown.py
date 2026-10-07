"""Facet filters are predicates of the upstream index (audit 2026-09-27, A-M6).

``search(query="soil moisture", sources=["zenodo","datacite"], published_after=2019,
published_before=2020)`` returned count=0, total=127525 and a next_cursor with no note:
the filters were applied only to the one ``size``-record window each stream returned,
never sent upstream, so ``total`` was the unfiltered upstream total and an empty page
read as "no matches". Zenodo and DataCite now evaluate the filters themselves; every
other source is still post-filtered, and says so in ``errors["filters"]`` when that
thinned the page.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _cursor, datacite, router, zenodo
from data_aggregator_mcp.models import DataResource

_ZENODO_EMPTY = {"hits": {"total": 0, "hits": []}}
_DATACITE_EMPTY = {"data": [], "meta": {"total": 0}}


# What a plain-words search sends each part of its title split (test_title_tier.py,
# test_deposit_tier.py). A kind filter sent upstream leaves the title matches whole.
_WORDS = "((soil OR soils) (moisture OR moistures))"


def _halves(field: str, deposits: str | None = None) -> tuple[str, ...]:
    clause = f'{field}:("soil moisture" OR "soil moistures")'
    titled = (
        [f"{_WORDS} AND {clause} AND {deposits}", f"{_WORDS} AND {clause} AND NOT {deposits}"]
        if deposits
        else [f"{_WORDS} AND {clause}"]
    )
    return (*titled, f"{_WORDS} AND NOT {clause}")


async def _sent(
    httpx_mock: HTTPXMock, body: dict, param: str, n: int, **kw
) -> tuple[httpx.Request, ...]:
    """Run a one-source search and return its ``n`` upstream requests: the deposits, the
    other title matches, then the rest."""
    httpx_mock.add_response(json=body, is_reusable=True)
    async with httpx.AsyncClient() as client:
        await router.search_page(client, query="soil moisture", **kw)
    reqs = httpx_mock.get_requests()
    assert len(reqs) == n
    return tuple(
        sorted(reqs, key=lambda r: (" AND NOT title" in (q := r.url.params[param]), " AND NOT " in q))
    )


def _with_halves(expected: str, field: str, deposits: str | None = None) -> list[str]:
    """``expected`` (the query with its filter clauses) for each part of the split."""
    assert expected.startswith(_WORDS)
    return [f"({half}){expected[len(_WORDS) :]}" for half in _halves(field, deposits)]


# --- the upstream requests carry the filters ----------------------------------------


@pytest.mark.parametrize(
    ("filters", "expected_q"),
    [
        (
            {"published_after": 2019, "published_before": 2020},
            "((soil OR soils) (moisture OR moistures)) AND publication_date:[2019-01-01 TO 2020-12-31]",
        ),
        (
            {"published_after": 2019},
            "((soil OR soils) (moisture OR moistures)) AND publication_date:[2019-01-01 TO *]",
        ),
        (
            {"kind": "software"},
            "((soil OR soils) (moisture OR moistures)) AND resource_type.type:(software)",
        ),
        # A-M5: dataset is its own mapped types, no longer the complement — an image or
        # a poster normalizes to "other", so it must not come back for kind=dataset.
        (
            {"kind": "dataset", "published_before": 2020},
            "((soil OR soils) (moisture OR moistures)) AND publication_date:[* TO 2020-12-31]"
            " AND resource_type.type:(dataset)",
        ),
    ],
)
async def test_zenodo_request_carries_the_filters(
    httpx_mock: HTTPXMock, filters: dict, expected_q: str
) -> None:
    deposits = None if "kind" in filters else zenodo.DEPOSIT_CLAUSE
    reqs = await _sent(
        httpx_mock, _ZENODO_EMPTY, "q", 2 if deposits is None else 3, sources=["zenodo"], **filters
    )
    assert [r.url.params["q"] for r in reqs] == _with_halves(expected_q, "title", deposits)


async def test_zenodo_request_without_filters_is_unchanged(httpx_mock: HTTPXMock) -> None:
    """Positive control: no filter clause, each half exactly as the title split sends it."""
    reqs = await _sent(httpx_mock, _ZENODO_EMPTY, "q", 3, sources=["zenodo"])
    assert [dict(r.url.params) for r in reqs] == [
        {"q": h, "size": "10"} for h in _halves("title", zenodo.DEPOSIT_CLAUSE)
    ]


@pytest.mark.parametrize(
    ("filters", "expected_query"),
    [
        (
            {"published_after": 2019, "published_before": 2020},
            "((soil OR soils) (moisture OR moistures)) AND publicationYear:[2019 TO 2020]",
        ),
        (
            {"published_before": 2020},
            "((soil OR soils) (moisture OR moistures)) AND publicationYear:[* TO 2020]",
        ),
        (
            {"kind": "software"},
            "((soil OR soils) (moisture OR moistures)) AND types.resourceTypeGeneral:(ComputationalNotebook OR Software)",
        ),
        (
            {"kind": "dataset"},
            "((soil OR soils) (moisture OR moistures)) AND types.resourceTypeGeneral:(Collection OR Dataset)",
        ),
        (
            {"kind": "publication", "published_after": 2019},
            "((soil OR soils) (moisture OR moistures)) AND publicationYear:[2019 TO *] AND types.resourceTypeGeneral:"
            "(Book OR BookChapter OR ConferencePaper OR Dissertation OR JournalArticle"
            " OR Preprint OR Report OR Text)",
        ),
    ],
)
async def test_datacite_request_carries_the_filters(
    httpx_mock: HTTPXMock, filters: dict, expected_query: str
) -> None:
    deposits = None if "kind" in filters else datacite.DEPOSIT_CLAUSE
    reqs = await _sent(
        httpx_mock,
        _DATACITE_EMPTY,
        "query",
        2 if deposits is None else 3,
        sources=["datacite"],
        **filters,
    )
    assert [r.url.params["query"] for r in reqs] == _with_halves(
        expected_query, "titles.title", deposits
    )


async def test_datacite_request_without_filters_is_unchanged(httpx_mock: HTTPXMock) -> None:
    """Positive control: no filter clause is added, each half with the relevance sort."""
    reqs = await _sent(httpx_mock, _DATACITE_EMPTY, "query", 3, sources=["datacite"])
    assert [dict(r.url.params) for r in reqs] == [
        {"query": h, "sort": "relevance", "page[size]": "10"}
        for h in _halves("titles.title", datacite.DEPOSIT_CLAUSE)
    ]


@pytest.mark.parametrize("adapter", [zenodo, datacite])
def test_a_kind_with_no_upstream_type_is_left_to_the_post_filter(adapter) -> None:
    """Neither archive has a study/sequencing_run type: pushing an invented one could
    only drop records, so it is not pushed (the year bounds still are)."""
    got = adapter.pushable({"published_after": 2019, "published_before": None, "kind": "study"})
    assert got == {"published_after": 2019}
    # Positive control: a mapped kind IS pushed, and a None filter is never "active".
    assert adapter.pushable({"kind": "software", "published_before": None}) == {"kind": "software"}


def test_unmapped_types_normalize_to_other_not_dataset() -> None:
    """A-M5: a type neither archive maps (a Zenodo image, a DataCite Image or Other, or no
    type at all) was normalized to kind="dataset", so figures and posters passed a
    kind=dataset filter. It is "other" now, and the upstream "other" is the complement of
    every mapped type — so the four kinds still partition the upstream (live test below).
    Positive control: mapped types keep their kinds."""
    from data_aggregator_mcp import _pushdown

    def zen(rtype: str | None) -> str:
        meta = {"title": "t", "resource_type": {"type": rtype} if rtype else {}}
        return zenodo._normalize({"id": 1, "metadata": meta}).kind

    def dc(rtg: str | None) -> str:
        types_ = {"resourceTypeGeneral": rtg} if rtg else {}
        return datacite._normalize({"attributes": {"doi": "10.1/x", "types": types_}}).kind

    assert [zen(t) for t in ("image", "poster", "other", None)] == ["other"] * 4
    assert [dc(t) for t in ("Image", "Audiovisual", "Other", None)] == ["other"] * 4
    assert [zen(t) for t in ("dataset", "software", "publication")] == [
        "dataset",
        "software",
        "publication",
    ]
    assert [dc(t) for t in ("Dataset", "Collection", "Software", "Text")] == [
        "dataset",
        "dataset",
        "software",
        "publication",
    ]
    assert _pushdown.kind_clause("t", zenodo._KIND_MAP, "other") == (
        "NOT t:(dataset OR publication OR software)"
    )


# --- sources that cannot push down say so -------------------------------------------


def _rec(src: str, i: int, year: int | None) -> DataResource:
    return DataResource(id=f"{src}:{i}", source=src, kind="dataset", title=f"{src} {i}", year=year)


def _post_only(recs: list[DataResource]):
    """An adapter with no pushdown: returns its window unfiltered."""

    async def search(client, q, *, size=10, offset=0):
        return len(recs), recs[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


def _pushing(recs: list[DataResource], seen: list, *, misdated: frozenset[str] = frozenset()):
    """A pushdown adapter whose upstream evaluates the year bounds — except for the ids
    in ``misdated``, which it returns anyway (an upstream that mis-dates a record)."""

    def pushable(filters, /):
        return {k: v for k, v in filters.items() if k in ("published_after", "published_before")}

    async def search(client, q, *, size=10, offset=0, filters=None):
        seen.append(filters)
        f = filters or {}
        lo, hi = f.get("published_after"), f.get("published_before")
        held = [
            r
            for r in recs
            if r.id in misdated
            or (
                r.year is not None and (lo is None or r.year >= lo) and (hi is None or r.year <= hi)
            )
            or (lo is None and hi is None)
        ]
        return len(held), held[offset : offset + size]

    return types.SimpleNamespace(search=search, pushable=pushable, PREFIXES=frozenset())


async def test_post_filtered_page_emptied_by_filters_carries_a_note(monkeypatch) -> None:
    """The A-M6 shape on a source that cannot push down: page 1 is all out of range."""
    held = [_rec("huggingface", i, 2010) for i in range(3)]
    held += [_rec("huggingface", i, 2019) for i in range(3, 6)]
    monkeypatch.setattr(router, "_ADAPTERS", {"huggingface": _post_only(held)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=3, published_after=2019)
        assert page.count == 0 and page.next_cursor is not None
        note = page.errors.get("filters", "")
        assert "huggingface" in note and "removed 3" in note and "UNFILTERED" in note, note
        assert "next_cursor continues" in note
        # The cursor does continue to the matches the note promised.
        page2 = await router.search_page(client, cursor=page.next_cursor)
    assert [r.id for r in page2.results] == [f"huggingface:{i}" for i in range(3, 6)]
    assert "filters" not in page2.errors  # nothing removed on this page -> no note


async def test_no_filters_note_when_nothing_was_removed(monkeypatch) -> None:
    """Positive controls: an in-range page, and an unfiltered search, carry no note."""
    held = [_rec("huggingface", i, 2019) for i in range(3)]
    monkeypatch.setattr(router, "_ADAPTERS", {"huggingface": _post_only(held)})
    async with httpx.AsyncClient() as client:
        in_range = await router.search_page(client, query="q", published_after=2019)
        unfiltered = await router.search_page(client, query="q")
    assert in_range.count == 3 and "filters" not in in_range.errors
    assert unfiltered.count == 3 and "filters" not in unfiltered.errors


async def test_filters_note_names_only_the_sources_that_did_not_push_down(monkeypatch) -> None:
    """zenodo pushed the filters (its total is filtered) — even though its upstream
    mis-dated one record the post-filter then removed, it is not named."""
    seen: list = []
    z = [_rec("zenodo", i, 2019) for i in range(3)] + [_rec("zenodo", 9, 2001)]
    hf = [_rec("huggingface", 0, 2001), _rec("huggingface", 1, 2019)]
    monkeypatch.setattr(
        router,
        "_ADAPTERS",
        {
            "zenodo": _pushing(z, seen, misdated=frozenset({"zenodo:9"})),
            "huggingface": _post_only(hf),
        },
    )
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", published_after=2019)
    assert seen == [{"published_after": 2019}]
    assert "zenodo:9" not in {r.id for r in page.results}  # the post-filter net held
    note = page.errors["filters"]
    assert "huggingface" in note and "zenodo" not in note and "removed 1 " in note, note

    # Positive control: with only pushdown sources there is nothing to report.
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _pushing(z, [], misdated=frozenset())})
    async with httpx.AsyncClient() as client:
        pushed_only = await router.search_page(client, query="q", published_after=2019)
    assert pushed_only.count == 3 and "filters" not in pushed_only.errors


async def test_cursor_minted_before_pushdown_continues_unpushed(monkeypatch) -> None:
    """Its offsets index the UNFILTERED upstream order; pushing the filters on page 2
    would index a different, shorter list and skip records."""
    seen: list = []
    z = [_rec("zenodo", i, 2019) for i in range(4)]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _pushing(z, seen)})
    filters = {"published_after": 2019, "published_before": None, "kind": None}
    old = _cursor.encode({"q": "q", "size": 2, "offsets": {"zenodo": 2}, "filters": filters})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, cursor=old)
        assert seen == [None]
        assert [r.id for r in page.results] == ["zenodo:2", "zenodo:3"]
        # Positive control: a fresh search pushes, and so does its own continuation.
        seen.clear()
        fresh = await router.search_page(client, query="q", size=2, published_after=2019)
        await router.search_page(client, cursor=fresh.next_cursor)
    assert seen == [{"published_after": 2019}] * 2


# --- live: the audit repro against the real upstreams ----------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_audit_repro_year_range_returns_in_range_records() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client,
            query="soil moisture",
            sources=["zenodo", "datacite"],
            published_after=2019,
            published_before=2020,
        )
    assert not {k for k in page.errors if k in ("zenodo", "datacite")}, page.errors
    assert page.count > 0, page.errors
    assert all(r.year is not None and 2019 <= r.year <= 2020 for r in page.results)
    # Unfiltered zenodo + datacite total on 2026-09-27: 127525 (91338 + 36187).
    assert 0 < page.total < 127_000, page.total  # the FILTERED total
    assert "filters" not in page.errors  # both streams pushed down


@live_only
async def test_live_kind_software_is_pushed_down() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client, query="soil moisture", sources=["zenodo", "datacite"], kind="software"
        )
    assert page.count > 0, page.errors
    assert {r.kind for r in page.results} == {"software"}
    assert 0 < page.total < 127_000, page.total
    assert "filters" not in page.errors


@live_only
@pytest.mark.parametrize("adapter", [zenodo, datacite])
async def test_live_kind_clauses_partition_the_upstream(adapter) -> None:
    """The pushdown invariant: the upstream kind predicate must keep every record the
    post-filter keeps. dataset + publication + software + other must add up to the
    unfiltered total exactly (other = no mapped type: images, posters, untyped, ...)."""
    async with httpx.AsyncClient(timeout=60) as client:
        whole, _ = await adapter.search(client, "soil moisture", size=1)
        parts = [
            (await adapter.search(client, "soil moisture", size=1, filters={"kind": k}))[0]
            for k in ("dataset", "publication", "software", "other")
        ]
    assert all(p > 0 for p in parts), parts
    assert sum(parts) == whole, (parts, whole)


def test_blank_query_sends_the_clauses_alone() -> None:
    """Review finding: a filters-only search (blank query) sent ``() AND ...``, which
    DataCite rejects with HTTP 400 ("Encountered ')'"). A blank query adds no clause of
    its own, so only the filter clauses go upstream. Positive control: a real query is
    still parenthesized so its top-level OR cannot capture a clause."""
    from data_aggregator_mcp import _pushdown

    clause = "publicationYear:[2019 TO 2020]"
    assert _pushdown.with_clauses("", [clause]) == clause
    assert _pushdown.with_clauses("   ", [clause]) == clause
    assert _pushdown.with_clauses("a OR b", [clause]) == f"(a OR b) AND {clause}"


@live_only
async def test_live_filters_only_search_is_accepted_by_both_upstreams() -> None:
    """A blank query with a year range: both upstreams answer (no HTTP 400) with records
    in range."""
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client,
            query="",
            sources=["zenodo", "datacite"],
            published_after=2019,
            published_before=2020,
        )
    assert not {k for k in page.errors if k in ("zenodo", "datacite")}, page.errors
    assert page.count > 0
    assert all(r.year is not None and 2019 <= r.year <= 2020 for r in page.results)
