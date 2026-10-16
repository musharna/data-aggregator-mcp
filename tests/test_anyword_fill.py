"""DataCite's records holding only some query words are searched below those holding all.

Round-3 diagnosis (2026-10-15): DataCite returns only records holding every query word
("honey bee images dataset" 92 hits, its words ORed 10.5M), and 10 of dam's 18 T4 queries
found nothing there. Over dam's own T4 queries, DataCite's top 50 reached 8 of the 24
DataCite-held key studies; with the words ORed it reached 16, keeping all 8.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import _cursor, datacite, router
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_Q = "honey bee pollen"
_FILL = "(honey OR bee OR pollen) AND NOT (honey bee pollen)"


def _hit(src: str, n: str, title: str) -> DataResource:
    return DataResource(id=f"{src}:{n}", source=src, kind="dataset", title=title)


def _source(sent: list[str], answers: dict[str, list[DataResource]], **flags):
    """A source answering each query from ``answers`` (anything else: nothing)."""

    async def search(client, q, *, size=10, offset=0, **kw):
        sent.append(q)
        hits = answers.get(q, answers.get("*", []))
        return len(hits) * (1000 if q == _FILL else 1), hits[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset(), **flags)


def _sources(
    monkeypatch, dc: list[str], loose: list[str], fill: int = 2, every_word: bool = True
) -> None:
    every = [_hit("dc", "e1", "honey bee pollen loads"), _hit("dc", "e2", "honey bee hive")]
    every = every if every_word else []
    some = [_hit("dc", "f1", "bee pollen images"), _hit("dc", "f2", "honey production")]
    some += [_hit("dc", f"f{i}", f"bee {i}") for i in range(3, fill + 1)]
    monkeypatch.setattr(
        router,
        "_ADAPTERS",
        {
            "dc": _source(
                dc, {_Q: every, _FILL: some}, REQUIRES_EVERY_WORD=True, ANY_WORD_FILL=True
            ),
            # Control: a source that does not declare the fill is sent the query alone.
            "loose": _source(loose, {"*": [_hit("loose", "l1", "pollen")]}),
        },
    )


async def test_hits_holding_some_words_rank_below_those_naming_more(monkeypatch) -> None:
    dc: list[str] = []
    loose: list[str] = []
    _sources(monkeypatch, dc, loose)
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=10)
    assert dc == [_Q, _FILL] and loose == [_Q]
    ids = [r.id for r in page.results]
    # 3 words named, then 2 (an every-word hit and a fill hit), then 1 (fill and loose).
    assert ids[0] == "dc:e1"
    assert set(ids[1:3]) == {"dc:e2", "dc:f1"} and set(ids[3:]) == {"dc:f2", "loose:l1"}
    by = {r.id: r for r in page.results}
    # An every-word hit not showing a word matched it unseen; a fill hit did not match it.
    assert by["dc:e2"].unshown_terms == ["pollen"]
    assert by["dc:f1"].unshown_terms == []
    # The fill's total (any record holding one word) is not the search's.
    assert page.total == 2 + 1


@pytest.mark.parametrize("query", ["pollen", "the pollen", "honey AND bee", '"honey bee"'])
async def test_no_fill_for_one_word_or_a_query_with_syntax(monkeypatch, query) -> None:
    dc: list[str] = []
    _sources(monkeypatch, dc, [])
    async with httpx.AsyncClient() as c:
        await router.search_page(c, query=query, size=10)
    assert dc == [query]


async def test_the_fill_is_paged_to_its_end_once(monkeypatch) -> None:
    dc: list[str] = []
    _sources(monkeypatch, dc, [], fill=24)
    seen: list[str] = []
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=10)
        seen += [r.id for r in page.results]
        while page.next_cursor:
            page = await router.search_page(c, cursor=page.next_cursor)
            seen += [r.id for r in page.results]
    want = {"dc:e1", "dc:e2", "loose:l1"} | {f"dc:f{i}" for i in range(1, 25)}
    assert sorted(seen) == sorted(want)


async def test_a_cursor_minted_before_the_fill_has_none(monkeypatch) -> None:
    dc: list[str] = []
    _sources(monkeypatch, dc, [], fill=24)
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=2)
        state = _cursor.decode(page.next_cursor)
        await router.search_page(c, cursor=page.next_cursor)
        # Its offsets index the sources' streams without the fill.
        state.pop("aw")
        await router.search_page(c, cursor=_cursor.encode(state))
    assert dc == [_Q, _FILL, _Q, _FILL, _Q]


async def test_a_source_holding_no_record_with_every_word_says_its_hits_hold_some(
    monkeypatch,
) -> None:
    # Live 0.73.0 (HyDaT query, 2026-10-16): total 0 beside 34 DataCite hits and nothing in
    # errors: the fill's hits counted as DataCite matching, so all_words was not given.
    dc: list[str] = []
    _sources(monkeypatch, dc, [], every_word=False)
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=10)
    assert {"dc:f1", "dc:f2"} <= {r.id for r in page.results}
    assert page.total == 1  # loose's one hit; the fill's are not counted
    assert page.errors["all_words"] == (
        "dc held no record with every word of the query, so its hits here hold only some "
        "of the words, and `total` does not count them"
    )
    # Positive control: the same search where dc holds records with every word.
    _sources(monkeypatch, dc, [])
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=10)
    assert "all_words" not in page.errors


async def test_every_word_sources_with_and_without_the_fill_are_named_apart(monkeypatch) -> None:
    _sources(monkeypatch, [], [], every_word=False)
    router._ADAPTERS["strict"] = _source([], {}, REQUIRES_EVERY_WORD=True)
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query=_Q, size=10)
    assert page.errors["all_words"] == (
        "strict matched nothing: it returns only records holding every word of the query, "
        "so a search with fewer words may find some; dc held no record with every word of "
        "the query, so its hits here hold only some of the words, and `total` does not "
        "count them"
    )
    two = router._all_words_note(["a", "b"], _Q, ["a", "b"])
    assert two == (
        "a, b held no record with every word of the query, so their hits here hold only some "
        "of the words, and `total` does not count them"
    )


def test_datacite_declares_every_word_and_the_fill() -> None:
    assert datacite.REQUIRES_EVERY_WORD is True and datacite.ANY_WORD_FILL is True


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_datacite_fill_finds_a_study_missing_one_query_word() -> None:
    # Round 3, T4 S21 (HyDaT, Monash Figshare): DataCite holds no record with all six words.
    q = "honeybee video annotated tracking dataset waggle"
    hydat = "datacite:10.26180/5f4c8d5815940"
    async with httpx.AsyncClient() as c:
        every, _ = await datacite.search(c, q, size=1)
        page = await router.search_page(c, query=q, sources=["datacite"], size=50)
    assert every == 0  # control: the words as written find nothing
    ids = [r.id.lower() for r in page.results]
    assert any(i.startswith(hydat) for i in ids), ids
    assert page.total == 0
    assert page.errors["all_words"].startswith("datacite held no record with every word")
