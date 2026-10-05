"""The default ranking puts hits that name the search above hits that do not.

The streams of a search were merged round-robin, so each source's first hit took a top
slot: for "transcriptome" in Orobanche aegyptiaca, a Pedicularis genome and a cobra
outranked the Phelipanche transcriptomes. Uses only names that existed before the
change, so it can be run against the old code."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import httpx
import pytest

from data_aggregator_mcp import router, taxonomy
from data_aggregator_mcp.models import DataResource

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_TAXON = taxonomy.TaxonInfo(
    taxid=78542,
    canonical_name="Phelipanche aegyptiaca",
    synonyms=("Orobanche aegyptiaca",),
    is_plant=True,
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(599)))


def _rec(id_: str, title: str) -> DataResource:
    return DataResource(id=id_, source=id_.split(":")[0], kind="dataset", title=title)


def _serve(monkeypatch, name: str, records: list[DataResource]) -> None:
    async def search(client, query, *, size, offset=0, **kw):
        return len(records), records[offset : offset + size]

    monkeypatch.setattr(router._ADAPTERS[name], "search", search)


# zenodo's hits come first in the round-robin, gbif's name the search.
_ZENODO = [
    _rec("zenodo:1", "A reference genome and transcriptome of Pedicularis groenlandica"),
    _rec("zenodo:2", "First records of Walterinnesia aegyptia"),
]
_GBIF = [
    _rec("gbif:1", "Transcriptome sequencing of Phelipanche aegyptiaca"),
    _rec("gbif:2", "Biocontrol of Orobanche aegyptiaca"),
]


async def test_hits_naming_more_of_the_search_rank_first(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_TAXON))
    _serve(monkeypatch, "zenodo", _ZENODO)
    _serve(monkeypatch, "gbif", _GBIF)
    async with _client() as client:
        page = await router.search_page(
            client,
            query="transcriptome",
            organism="Orobanche aegyptiaca",
            sources=["zenodo", "gbif"],
        )
    # organism + query, organism only, query only, neither.
    assert [r.id for r in page.results] == ["gbif:1", "gbif:2", "zenodo:1", "zenodo:2"]


async def test_hits_that_name_as_much_keep_the_round_robin_order(monkeypatch) -> None:
    _serve(monkeypatch, "zenodo", _ZENODO)
    _serve(monkeypatch, "gbif", _GBIF)
    async with _client() as client:
        page = await router.search_page(client, query="sequencing", sources=["zenodo", "gbif"])
    # Only gbif:1 names "sequencing"; the other three tie and stay in round-robin order.
    assert [r.id for r in page.results] == ["gbif:1", "zenodo:1", "zenodo:2", "gbif:2"]


async def test_paging_past_reordered_hits_loses_and_repeats_nothing(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_TAXON))
    _serve(monkeypatch, "zenodo", _ZENODO)
    _serve(monkeypatch, "gbif", _GBIF)
    seen: list[list[str]] = []
    async with _client() as client:
        page = await router.search_page(
            client,
            query="transcriptome",
            organism="Orobanche aegyptiaca",
            sources=["zenodo", "gbif"],
            size=1,
        )
        seen.append([r.id for r in page.results])
        while page.next_cursor:
            page = await router.search_page(client, cursor=page.next_cursor)
            seen.append([r.id for r in page.results])
    assert seen == [["gbif:1"], ["gbif:2"], ["zenodo:1"], ["zenodo:2"]]


async def test_a_multi_query_page_without_embeddings_keeps_the_tiers(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_TAXON))
    monkeypatch.setattr(
        router.query_understanding_mod, "expand", AsyncMock(return_value=["RNA-seq"])
    )
    monkeypatch.setattr(router.embeddings, "embed", AsyncMock(return_value=None))
    _serve(monkeypatch, "zenodo", _ZENODO)
    _serve(monkeypatch, "gbif", _GBIF)
    async with _client() as client:
        page = await router.search_page(
            client,
            query="transcriptome",
            organism="Orobanche aegyptiaca",
            sources=["zenodo", "gbif"],
            multi_query=True,
        )
    assert "semantic" in page.errors
    assert [r.id for r in page.results] == ["gbif:1", "gbif:2", "zenodo:1", "zenodo:2"]


def _names_organism(r: DataResource) -> bool:
    text = " ".join(
        [r.title, r.description or "", *r.subjects, *r.organism, *(t.name for t in r.taxa)]
    )
    return "aegyptiaca" in text.casefold()


@pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_top_hits_name_the_organism_asked_for() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client, query="transcriptome", organism="Orobanche aegyptiaca", size=10
        )
    top = page.results[:5]
    assert len(top) == 5
    assert all(_names_organism(r) for r in top), [(r.source, r.title[:60]) for r in top]
    assert len({r.source for r in top}) > 1  # ranked across sources, not one source's list
