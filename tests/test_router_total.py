"""``total`` says what it adds up.

MCP-server audit (2026-11-10): "Phelipanche aegyptiaca RNA-seq" over every source gave
``total`` 66,402, with nothing saying what it counted. Zenodo reported 22,121, BioStudies
36,163 and DataONE 8,115, counting records that hold any of the words; DataCite, which
needs every word, reported 4. Read as a match count, the sum is wrong by two orders of
magnitude.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


def _adapter(src: str, n: int, *, every_word: bool, fail: bool = False):
    async def search(client, q, *, size=10, offset=0):
        if fail:
            raise RuntimeError("upstream down")
        hits = [
            DataResource(id=f"{src}:{i}", source=src, kind="dataset", title=f"{src} {i}")
            for i in range(offset, min(n, offset + size, 3))
        ]
        return n, hits

    return types.SimpleNamespace(
        search=search, PREFIXES=frozenset(), REQUIRES_EVERY_WORD=every_word
    )


async def _page(monkeypatch, adapters, query="Phelipanche aegyptiaca RNA-seq"):
    monkeypatch.setattr(router, "_ADAPTERS", adapters)
    async with httpx.AsyncClient() as client:
        return await router.search_page(client, query=query, sources=list(adapters))


_AUDIT = {
    "zenodo": (22121, False),
    "datacite": (4, True),
    "dataone": (8115, False),
    "biostudies": (36163, False),
}


async def test_total_names_each_sources_count_and_which_need_every_word(monkeypatch) -> None:
    page = await _page(
        monkeypatch, {s: _adapter(s, n, every_word=ew) for s, (n, ew) in _AUDIT.items()}
    )
    assert page.total == 66403
    assert page.errors["total"] == (
        "`total` adds up the count each source reports, so it is not a count of distinct "
        "hits: biostudies 36163, zenodo 22121, dataone 8115, datacite 4. biostudies, "
        "zenodo, dataone may count records holding any one of the query words, not all; "
        "datacite counts only records holding every word"
    )


async def test_the_total_note_leaves_out_what_did_not_add_to_it(monkeypatch) -> None:
    # A failed source and one that found nothing add nothing to `total`, so they are not
    # named.
    adapters = {
        "zenodo": _adapter("zenodo", 7, every_word=False),
        "gbif": _adapter("gbif", 0, every_word=False),
        "dataone": _adapter("dataone", 5, every_word=False, fail=True),
    }
    page = await _page(monkeypatch, adapters, query="tardigrade tun")
    assert page.total == 7 and "dataone" in page.errors
    assert page.errors["total"] == (
        "`total` adds up the count each source reports, so it is not a count of distinct "
        "hits: zenodo 7. zenodo may count records holding any one of the query words, not all"
    )
    # A one-word query has no "any one of the words": no note, so a clean search keeps
    # errors empty.
    page = await _page(monkeypatch, adapters, query="tardigrade")
    assert page.total == 7 and "total" not in page.errors
    # Nor when every source counted needs every word.
    page = await _page(monkeypatch, {"datacite": _adapter("datacite", 4, every_word=True)})
    assert page.total == 4 and page.errors == {}


async def test_no_total_note_when_nothing_was_counted(monkeypatch) -> None:
    page = await _page(monkeypatch, {"gbif": _adapter("gbif", 0, every_word=False)})
    assert page.total == 0 and "total" not in page.errors
    # Positive control: one hit counted is named.
    page = await _page(monkeypatch, {"gbif": _adapter("gbif", 1, every_word=False)})
    assert page.errors["total"].startswith("`total` adds up the count each source reports")


_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live
async def test_live_the_total_note_adds_up_to_total() -> None:
    async with httpx.AsyncClient(timeout=90) as client:
        page = await router.search_page(
            client, query="Phelipanche aegyptiaca RNA-seq", sources=["zenodo", "datacite"]
        )
    note = page.errors["total"]
    counts = note.split(": ", 1)[1].split(". ")[0]
    numbers = [int(part.rsplit(" ", 1)[1]) for part in counts.split(", ")]
    assert sum(numbers) == page.total, (note, page.total)
    assert note.endswith("datacite counts only records holding every word"), note
