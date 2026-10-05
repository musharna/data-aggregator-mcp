"""The default ranking puts hits that name the search above hits that do not.

The streams of a search were merged round-robin, so each source's first hit took a top
slot: for "transcriptome" in Orobanche aegyptiaca, a Pedicularis genome and a cobra
outranked the Phelipanche transcriptomes. Uses only names that existed before the
change, so it can be run against the old code."""

from __future__ import annotations

import os
import re
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


# --- the query as written, and a deposit's copies (2026-10-05, round-2 snow leopard task)


async def test_hits_naming_the_query_as_written_outrank_its_words_scattered(monkeypatch) -> None:
    """Live: Antarctic station logs ("snow" ... "leopard seals") tied the snow leopard
    studies on two words each and took a share of every page by round-robin."""
    log = DataResource(
        id="nasacmr:1", source="nasacmr", kind="dataset",
        title="Log of biological observations at Casey, 1974",
        description="Snow petrels nested near the hut; leopard seals hauled out.",
    )  # fmt: skip
    study = _rec("gbif:1", "Snow leopard camera-trap survey in the Altai")
    _serve(monkeypatch, "nasacmr", [log])
    _serve(monkeypatch, "gbif", [study])
    async with _client() as client:
        page = await router.search_page(
            client, query="snow leopard", size=1, sources=["nasacmr", "gbif"]
        )
        assert [r.id for r in page.results] == ["gbif:1"]
        # positive control: the log is not lost, only later
        nxt = await router.search_page(client, cursor=page.next_cursor)
    assert [r.id for r in nxt.results] == ["nasacmr:1"]


@pytest.mark.parametrize("described", ["datacite", "dataone"])
async def test_a_deposit_ranks_by_its_best_described_copy(monkeypatch, described) -> None:
    """The DOI dedup keeps the most fetchable copy (DataONE's), which has no description;
    ranked on its own text it sank below hits naming nothing, and its DataCite twin, never
    handled, held DataCite's offset in place for every later page. Either copy may be the
    described one, so the rank is the best copy's, not the last one seen."""
    doi = "10.5061/dryad.66t1g1k2t"
    title = "Data from: Age estimation using methylation-sensitive markers"
    about = {"description": "Faecal DNA of wild snow leopards in Mongolia."}
    copy = DataResource(
        id="dataone:sha256:1", source="dataone", kind="dataset", title=title, doi=doi,
        **(about if described == "dataone" else {}),
    )  # fmt: skip
    twin = DataResource(
        id=f"datacite:{doi}", source="dryad", kind="dataset", title=title, doi=doi.upper(),
        **(about if described == "datacite" else {}),
    )  # fmt: skip
    log = DataResource(
        id="nasacmr:1", source="nasacmr", kind="dataset", title="Casey station log",
        description="Snow petrels and leopard seals.",
    )  # fmt: skip
    _serve(monkeypatch, "nasacmr", [log])
    _serve(monkeypatch, "dataone", [copy])
    _serve(monkeypatch, "datacite", [twin])
    async with _client() as client:
        page = await router.search_page(
            client, query="snow leopard", size=1, sources=["nasacmr", "dataone", "datacite"]
        )
    assert [r.id for r in page.results] == ["dataone:sha256:1"]  # the kept copy, first
    # both copies are handled, so DataCite moves on
    assert router._cursor.decode(page.next_cursor)["offsets"]["datacite"] == 1


@pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_a_first_page_for_a_two_word_name_is_hits_naming_it() -> None:
    """Round 2's lost ground, replayed: on 0.61.0 the first page for "snow leopard"
    held Antarctic logs and museum collections tied with the studies."""
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(client, query="snow leopard", size=50, kind="dataset")

    def words(r: DataResource) -> str:  # "imnet1k_snow_leopard_ounce" names it too
        return " ".join(re.split(r"[\W_]+", " ".join([r.title, r.description or "", *r.subjects])))

    named = [r for r in page.results if "snow leopard" in words(r).casefold()]
    assert len(page.results) >= 20 and len(named) == len(page.results), [
        r.title for r in page.results if r not in named
    ]
