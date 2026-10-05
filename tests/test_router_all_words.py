"""A source that needs every query word, and matched nothing, is named in errors.

Head-to-head round 2 (2026-10-05): NCBI omics and OmicsDI AND every word, so a long query
came back from them empty with nothing in errors{}, and the agent read the 0 as "no such
data". GEO: "tardigrade" 221, "tun" 464, "tardigrade dehydration tun" 0.
"""

from __future__ import annotations

import os
import types
from unittest.mock import AsyncMock

import httpx
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


def _hit(src: str, i: int = 0) -> DataResource:
    return DataResource(id=f"{src}:{i}", source=src, kind="dataset", title=f"{src} {i}")


def _adapter(n: int, *, every_word: bool, fail: bool = False):
    async def search(client, q, *, size=10, offset=0):
        if fail:
            raise RuntimeError("upstream down")
        return n, [_hit("x", i) for i in range(offset, min(n, offset + size))]

    return types.SimpleNamespace(
        search=search, PREFIXES=frozenset(), REQUIRES_EVERY_WORD=every_word
    )


def _composite(counts: dict[str, int]):
    async def search_subsource(client, sub, q, *, size=10, offset=0):
        return counts[sub], [_hit(sub, i) for i in range(counts[sub])][offset : offset + size]

    return types.SimpleNamespace(
        SUBSOURCES=tuple(counts),
        search_subsource=search_subsource,
        PREFIXES=frozenset(),
        REQUIRES_EVERY_WORD=True,
    )


async def _errors(monkeypatch, adapters, query="tardigrade dehydration tun", **kw) -> dict:
    monkeypatch.setattr(router, "_ADAPTERS", adapters)
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query=query, sources=list(adapters), **kw)
    return page.errors


async def test_an_empty_every_word_source_is_named_and_others_are_not(monkeypatch) -> None:
    errors = await _errors(
        monkeypatch,
        {
            "omicsdi": _adapter(0, every_word=True),
            "zenodo": _adapter(0, every_word=False),  # empty, but matches any word
            "gbif": _adapter(3, every_word=False),
        },
    )
    assert errors["all_words"] == (
        "omicsdi matched nothing: it returns only records holding every word of the query, "
        "so a search with fewer words may find some"
    )


async def test_no_note_when_the_every_word_source_found_something(monkeypatch) -> None:
    """Positive control: same query and sources, but the every-word source has hits."""
    errors = await _errors(
        monkeypatch,
        {"omicsdi": _adapter(2, every_word=True), "zenodo": _adapter(0, every_word=False)},
    )
    assert "all_words" not in errors


async def test_no_note_for_a_one_word_query(monkeypatch) -> None:
    errors = await _errors(monkeypatch, {"omicsdi": _adapter(0, every_word=True)}, query="tun")
    assert "all_words" not in errors


async def test_a_failed_stream_is_an_error_not_an_empty_match(monkeypatch) -> None:
    errors = await _errors(
        monkeypatch,
        {
            "omicsdi": _adapter(0, every_word=True, fail=True),
            "omics": _composite({"geo": 0, "sra": 4}),
        },
    )
    assert errors["omicsdi"] == "RuntimeError: upstream down"
    # each NCBI db is its own stream: only the empty one is named
    assert errors["all_words"].startswith("omics/geo matched nothing: it returns ")


async def test_several_empty_sources_are_named_together(monkeypatch) -> None:
    errors = await _errors(
        monkeypatch,
        {"omicsdi": _adapter(0, every_word=True), "omics": _composite({"geo": 0, "sra": 0})},
    )
    assert errors["all_words"].startswith("omicsdi, omics/geo, omics/sra matched nothing: they ")


async def test_multi_query_names_a_source_only_if_every_variant_was_empty(monkeypatch) -> None:
    calls: list[str] = []

    async def search(client, q, *, size=10, offset=0):
        calls.append(q)
        n = 2 if q == "tardigrade" else 0
        return n, [_hit("omicsdi", i) for i in range(n)]

    every_word = types.SimpleNamespace(
        search=search, PREFIXES=frozenset(), REQUIRES_EVERY_WORD=True
    )
    monkeypatch.setattr(
        router.query_understanding_mod, "expand", AsyncMock(return_value=["tardigrade"])
    )
    errors = await _errors(monkeypatch, {"omicsdi": every_word}, multi_query=True)
    assert sorted(calls) == ["tardigrade", "tardigrade dehydration tun"]  # both variants ran
    assert "all_words" not in errors
    # and when the second variant is empty too, the source is named once
    monkeypatch.setattr(
        router.query_understanding_mod, "expand", AsyncMock(return_value=["tardigrade tun"])
    )
    errors = await _errors(monkeypatch, {"omicsdi": every_word}, multi_query=True)
    assert errors["all_words"].startswith("omicsdi matched nothing: it returns ")


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_an_over_long_geo_query_says_why_it_is_empty() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        long = await router.search_page(
            client, query="tardigrade dehydration tun", sources=["omics"]
        )
        short = await router.search_page(client, query="tardigrade", sources=["omics"])
    assert "omics/geo" in long.errors["all_words"]
    # positive control: one word of the same query finds GEO records
    assert any(r.id.startswith("geo:") for r in short.results)
    assert "all_words" not in short.errors
