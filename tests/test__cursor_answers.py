"""A cursor comes back from the client and is not signed: any state can be sent.

Each test forges a cursor from a genuine one (minted by the router on a real page 1)
and asserts the forged state is refused before any upstream call, next to the genuine
cursor continuing in the same test.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from data_aggregator_mcp import _cursor, router
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import SearchResult
from tests.test_router_observed import _client, _serve

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# Every JSON type, plus the values each check sits on the edge of.
_WRONG: list[Any] = [None, True, 0, -1, 1.5, "x", "", [], ["x"], [1], {}, {"k": 1}]


async def _genuine(monkeypatch, *, multi_query: bool) -> tuple[dict, list]:
    """Page 2's own cursor (it carries ``ahead`` and every setting), from fake upstreams."""
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    ups = [_serve(monkeypatch, name) for name in ("zenodo", "datacite")]
    async with _client() as client:
        p1 = await router.search_page(
            client,
            query="rna",
            size=3,
            sources=["zenodo", "datacite"],
            published_after=2000,
            kind="dataset",
            multi_query=multi_query,
        )
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    assert p2.next_cursor is not None
    return _cursor.decode(p2.next_cursor), ups


async def _continue(state: dict) -> SearchResult:
    async with _client() as client:
        return await router.search_page(client, cursor=_cursor.encode(state))


def _paths(obj: Any, prefix: tuple = ()) -> Iterator[tuple]:
    """Every key and list position in a decoded cursor, depth first."""
    if prefix:
        yield prefix
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _paths(v, (*prefix, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _paths(v, (*prefix, i))


def _replace(state: dict, path: tuple, value: Any) -> dict:
    out = copy.deepcopy(state)
    target = out
    for p in path[:-1]:
        target = target[p]
    target[path[-1]] = value
    return out


@pytest.mark.parametrize("multi_query", [False, True], ids=["single", "multi"])
async def test_no_wrong_typed_cursor_field_escapes_as_a_bare_error(monkeypatch, multi_query):
    """Every field of a genuine cursor, set to every JSON type, is either refused as a
    corrupt cursor or read cleanly. A wrong-typed `filters`, year bound or `sources`
    escaped as a bare AttributeError/TypeError from inside the search (34 for each
    cursor shape on 8c70c70). An unknown source name is refused with the same ValueError a fresh search
    gives it, which is not a cursor-shape failure."""
    state, _ = await _genuine(monkeypatch, multi_query=multi_query)
    assert isinstance(await _continue(state), SearchResult)  # the genuine cursor continues
    escapes = []
    for path in list(_paths(state)):
        for value in _WRONG:
            try:
                await _continue(_replace(state, path, value))
            except ValidationError:
                pass  # refused: by decode, or by the router as a fresh search would be
            except ValueError as exc:
                if path[0] != "sources" or not str(exc).startswith("unknown source "):
                    escapes.append((path, value, f"{type(exc).__name__}: {exc}"))
            except Exception as exc:  # noqa: BLE001 - collecting every escape is the test
                escapes.append((path, value, f"{type(exc).__name__}: {exc}"))
    assert escapes == []


async def test_a_cursor_cannot_fan_out_more_variants_than_a_search_mints(monkeypatch):
    """Each variant is a fan-out to every selected source. A search mints at most
    MAX_QUERY_VARIANTS; a forged cursor listing 200 made 200 requests per source in one
    call, and one listing none searched nothing and answered with an empty success."""
    state, ups = await _genuine(monkeypatch, multi_query=True)
    for up in ups:
        up.calls.clear()
    await _continue(state)
    assert len(ups[0].calls) == len(state["variants"]) == 2  # genuine: one call per variant

    cap = router.MAX_QUERY_VARIANTS
    at_cap = [f"v{i}" for i in range(cap)]
    for up in ups:
        up.calls.clear()
    await _continue({**state, "variants": at_cap, "raw_variants": at_cap})
    assert len(ups[0].calls) == cap  # the most a search can mint still continues

    for variants in ([f"v{i}" for i in range(cap + 1)], [f"v{i}" for i in range(200)], []):
        for up in ups:
            up.calls.clear()
        forged = {**state, "variants": variants, "raw_variants": variants}
        with pytest.raises(
            ValidationError,
            match=r"^\[ValidationError\] invalid or corrupt cursor: "
            r"'variants' must be a list of 1 to 4 strings$",
        ):
            await _continue(forged)
        assert [up.calls for up in ups] == [[], []]  # refused before any request


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"kind": "nope"}, r"'kind' must be one of \['dataset', 'publication', "),
        ({"kind": "other"}, r"'kind' must be one of \['dataset', 'publication', "),
        ({"published_after": "2000"}, r"year filters must be integers$"),
        ({"published_before": True}, r"year filters must be integers$"),
        ({"published_after": 2000, "organism": "x"}, r"'filters' may hold only "),
        ([], r"'filters' may hold only published_after, published_before and kind$"),
    ],
)
async def test_a_cursor_cannot_ask_for_a_filter_a_search_refuses(monkeypatch, filters, message):
    """A fresh search refuses an unknown kind; a cursor carrying one searched, threw
    every record away in the post-filter and answered with an empty page."""
    state, ups = await _genuine(monkeypatch, multi_query=False)
    page = await _continue(state)  # the genuine filters continue
    assert page.results and all(r.kind == "dataset" for r in page.results)
    for up in ups:
        up.calls.clear()
    with pytest.raises(
        ValidationError, match=r"^\[ValidationError\] invalid or corrupt cursor: " + message
    ):
        await _continue({**state, "filters": filters})
    assert [up.calls for up in ups] == [[], []]


async def test_every_genuine_cursor_shape_still_decodes(monkeypatch):
    """The checks above admit every state a search mints, including the legacy ones the
    router continues: no filters, null filters, an unset kind, and a multi-query cursor
    without raw_variants."""
    single, _ = await _genuine(monkeypatch, multi_query=False)
    multi, _ = await _genuine(monkeypatch, multi_query=True)
    legacy = {"q": "rna", "size": 2, "offsets": {"zenodo": 2}}
    for state in (
        single,
        multi,
        legacy,
        {**legacy, "filters": None},
        {**legacy, "filters": {"kind": None, "published_after": None, "published_before": 1}},
        {**legacy, "variants": ["rna", "alt"]},
        {**legacy, "sources": None},
    ):
        assert _cursor.decode(_cursor.encode(state)) == state


@_live_only
async def test_live_page_two_continues_from_the_real_cursor_and_a_forged_one_is_refused():
    """Real execution: page 1 of a live Zenodo search through the router, page 2 from
    its own cursor (different records), then the same cursor forged two ways is refused
    before any request is made."""
    async with httpx.AsyncClient(timeout=60) as client:
        p1 = await router.search_page(client, query="soil moisture", size=3, sources=["zenodo"])
        assert p1.errors == {} and len(p1.results) == 3 and p1.next_cursor
        p2 = await router.search_page(client, cursor=p1.next_cursor)
        assert p2.errors == {} and len(p2.results) == 3
        assert {r.id for r in p1.results}.isdisjoint(r.id for r in p2.results)

        state = _cursor.decode(p1.next_cursor)
        sent: list[httpx.Request] = []

        async def record(request: httpx.Request) -> None:
            sent.append(request)

        client.event_hooks["request"].append(record)
        variants = [f"v{i}" for i in range(router.MAX_QUERY_VARIANTS + 1)]
        for forged in (
            {**state, "variants": variants, "raw_variants": variants},
            {**state, "filters": {"published_after": "2000"}},
        ):
            with pytest.raises(ValidationError, match=r"invalid or corrupt cursor: "):
                await router.search_page(client, cursor=_cursor.encode(forged))
        assert sent == []
        # The positive control on the same hooked client: the genuine cursor still sends.
        again = await router.search_page(client, cursor=p1.next_cursor)
        assert again.errors == {} and len(again.results) == 3 and sent
