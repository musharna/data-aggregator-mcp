"""A search page fits in one tool result, and hits cut to make it fit come next page.

Head-to-head round 2 (2026-10-05): 9 of this server's 95 tool results were longer than
Claude Code passes to the model (about 50,000 characters) and were saved to a file the
agent had no tool to read. A 50-hit page ran 57k to 427k characters: a search hit's
lists were uncapped (one GBIF hit carried 400k characters of links) and a page had no
size bound, only a hit count.
"""

from __future__ import annotations

import json
import os
import types

import httpx
import pytest

from data_aggregator_mcp import router, server, taxonomy
from data_aggregator_mcp.models import (
    SEARCH_LIST_LIMITS,
    Creator,
    DataResource,
    Link,
    compact,
)

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
# What Claude Code passes to the model from one tool result (round 2: 49,606 characters
# went through, 50,493 did not).
HOST_LIMIT = 50_000


def _adapter(recs: list[DataResource]):
    async def search(client, q, *, size=10, offset=0):
        return len(recs), [compact(r) for r in recs[offset : offset + size]]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


def _big(i: int) -> DataResource:
    """A hit at the size real Zenodo/Dryad hits reach once compacted (~1.5k)."""
    return DataResource(
        id=f"zenodo:{i}",
        source="zenodo",
        kind="dataset",
        title=f"Snow leopard camera-trap survey {i} " + "x" * 120,
        description="d" * 2_000,
        creators=[Creator(name=f"Author {j}", orcid="0000-0002-1825-0097") for j in range(8)],
        subjects=[f"subject {j}" for j in range(12)],
        links=[Link(rel="has_version", target_id=f"zenodo:{i}{j}") for j in range(9)],
    )


async def _walk(first: dict) -> tuple[list[str], list]:
    ids: list[str] = []
    pages = []
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, **first)
        pages.append(page)
        while True:
            ids += [r.id for r in page.results]
            if not page.next_cursor or len(pages) > 20:
                break
            page = await router.search_page(client, cursor=page.next_cursor)
            pages.append(page)
    return ids, pages


async def test_a_page_of_large_hits_fits_one_tool_result_and_loses_no_hit(monkeypatch) -> None:
    held = [_big(i) for i in range(60)]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(held)})
    ids, pages = await _walk({"query": "snow leopard", "size": 50, "sources": ["zenodo"]})

    first = pages[0]
    assert len(json.dumps(first.model_dump(), separators=(",", ":"))) < HOST_LIMIT
    assert 0 < first.count < 50  # cut to fit, not to nothing
    assert first.errors["page_size"].startswith(f"{first.count} of the 50 hits asked for fit")
    assert first.next_cursor
    # Nothing cut to fit is lost or repeated: the walk returns every hit exactly once.
    assert sorted(ids) == sorted(r.id for r in held) and len(ids) == len(set(ids))
    for page in pages:
        assert len(json.dumps(page.model_dump(), separators=(",", ":"))) < HOST_LIMIT


async def test_a_page_of_small_hits_is_not_cut(monkeypatch) -> None:
    """Positive control: 50 hits that fit come back as one page with no note."""
    held = [
        DataResource(id=f"zenodo:{i}", source="zenodo", kind="dataset", title=f"t {i}")
        for i in range(60)
    ]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(held)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=50, sources=["zenodo"])
    assert page.count == 50 and "page_size" not in page.errors


async def test_one_hit_with_a_huge_link_list_reaches_the_agent_capped(monkeypatch) -> None:
    huge = DataResource(
        id="gbif:1",
        source="gbif",
        kind="dataset",
        title="occurrences",
        links=[Link(rel="references", target_id=f"10.15468/dl.{j:06d}") for j in range(5_000)],
    )
    small = DataResource(
        id="gbif:2",
        source="gbif",
        kind="dataset",
        title="small",
        links=[Link(rel="references", target_id="10.15468/dl.a")],
    )
    monkeypatch.setattr(router, "_ADAPTERS", {"gbif": _adapter([huge, small])})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=10, sources=["gbif"])
    capped, kept = page.results
    assert len(capped.links) == SEARCH_LIST_LIMITS["links"]
    assert capped.truncated["links"] == "first 5 of 5000 in this search hit; resolve for all"
    assert kept.links == small.links and kept.truncated == {}  # under the cap: untouched
    assert len(json.dumps(page.model_dump(), separators=(",", ":"))) < 10_000


async def test_lists_enrichment_grows_are_capped_again(monkeypatch) -> None:
    """Taxon enrichment runs after the adapter compacted the hit; a plant organism adds
    a cross-link, which must not take the hit past its link cap."""
    rec = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        organism=["Phelipanche aegyptiaca"],
        links=[Link(rel="has_version", target_id=f"zenodo:1{j}") for j in range(5)],
    )

    async def fake_resolve_taxon(client, name):
        return taxonomy.TaxonInfo(
            taxid=99112, canonical_name="Phelipanche aegyptiaca", synonyms=(), is_plant=True
        )

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve_taxon)
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter([rec])})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=10, sources=["zenodo"])
    (hit,) = page.results
    assert [t.taxid for t in hit.taxa] == [99112]  # enrichment did run
    assert len(hit.links) == 5
    assert hit.truncated["links"] == "first 5 of 6 in this search hit; resolve for all"


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_a_50_hit_search_fits_one_tool_result() -> None:
    """Round 2's lost search, replayed: 57,667 characters on 0.60.0."""
    result = await server._dispatch("search", {"query": "snow leopard Panthera uncia", "size": 50})
    assert len(json.dumps(result, separators=(",", ":"))) < HOST_LIMIT
    assert result["count"] >= 20 and result["next_cursor"]


async def test_a_hit_carries_only_the_fields_it_has(monkeypatch) -> None:
    """R4 re-run (2026-10-06): a hit spent 27% of its characters on null and empty
    fields, 500 characters for a hit with none, so a 50-hit search returned 34."""
    held = [
        DataResource(
            id=f"zenodo:{i}",
            source="zenodo",
            kind="dataset",
            title=f"Snow leopard survey {i} " + "x" * 580,
            year=2020,
            creators=[Creator(name="A")],
            is_latest=False,
        )
        for i in range(60)
    ]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(held)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=50, sources=["zenodo"])

    assert page.count == 50 and "page_size" not in page.errors
    hits = page.model_dump(mode="json")["results"]
    # Every field with a value stays, a false one included; no null, [] or {} does.
    assert hits[0] == {
        "id": "zenodo:0",
        "source": "zenodo",
        "kind": "dataset",
        "title": held[0].title,
        "year": 2020,
        "creators": [{"name": "A"}],
        "is_latest": False,
    }
    assert json.loads(page.model_dump_json())["results"] == hits
    # The hits read back as the records they were.
    assert [DataResource.model_validate(h) for h in hits] == [compact(r) for r in held[:50]]


async def test_the_budget_counts_a_hit_as_it_is_sent(monkeypatch) -> None:
    """The page budget measures each hit in the form the page sends: no spaces, no
    escaped accents, no empty fields. Measured any other way, it cuts pages short."""
    held = [
        DataResource(
            id=f"zenodo:{i}",
            source="zenodo",
            kind="dataset",
            title=f"Léopard des neiges {i}",
            creators=[Creator(name="Ö")],
            subjects=["Panthera uncia"],
        )
        for i in range(3)
    ]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(held)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="q", size=10, sources=["zenodo"])
    sent = json.loads(page.model_dump_json())["results"]
    assert len(sent) == 3
    for hit, r in zip(sent, page.results, strict=True):
        assert router._hit_chars(r) == len(
            json.dumps(hit, separators=(",", ":"), ensure_ascii=False)
        )
