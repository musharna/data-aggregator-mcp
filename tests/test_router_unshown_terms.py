"""A hit from a source that matches every query word names the words it does not show.

Head-to-head round 2 on 0.66.0 (2026-10-06): OmicsDI returned PXD055071 for "Chlamydomonas
nitrogen" in both R5 runs, but its title and description say only "nutrient stress";
the nitrogen depletion is in its sample protocol. Shown nothing that said why it
matched, the agent left a qualifying study out both times.
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp.models import DataResource, compact

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"

_SILENT = "Extracellular vesicles in Chlamydomonas: mediators of nutrient sensing"
_NAMES_ALL = "Chlamydomonas proteome after nitrogen deprivation"


def _adapter(src: str, titles: list[str], *, every_word: bool):
    async def search(client, q, *, size=10, offset=0):
        hits = [
            DataResource(id=f"{src}:{i}", source=src, kind="study", title=t)
            for i, t in enumerate(titles)
        ]
        return len(hits), hits[offset : offset + size]

    return types.SimpleNamespace(
        search=search, PREFIXES=frozenset(), REQUIRES_EVERY_WORD=every_word
    )


async def _hits(monkeypatch, adapters, query="Chlamydomonas nitrogen") -> dict[str, list[str]]:
    monkeypatch.setattr(router, "_ADAPTERS", adapters)
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query=query, sources=list(adapters), size=10)
    return {r.id: r.unshown_terms for r in page.results}


async def test_an_every_word_hit_names_the_words_it_does_not_show(monkeypatch) -> None:
    hits = await _hits(
        monkeypatch,
        {
            "omicsdi": _adapter("omicsdi", [_SILENT, _NAMES_ALL], every_word=True),
            # The same silent title from a source that matches any word: its hit may
            # really lack the word, so it claims nothing.
            "zenodo": _adapter("zenodo", [_SILENT], every_word=False),
        },
    )
    assert hits["omicsdi:0"] == ["nitrogen"]
    # Positive controls: a hit that shows every word, and the any-word source's hit.
    assert hits["omicsdi:1"] == []
    assert hits["zenodo:0"] == []


async def test_a_hit_of_a_composite_every_word_source_names_them_too(monkeypatch) -> None:
    """NCBI omics is one adapter of three streams (GEO, SRA, BioProject)."""

    async def search_subsource(client, sub, q, *, size=10, offset=0):
        hit = DataResource(id=f"{sub}:0", source=sub, kind="study", title=_SILENT)
        return 1, [hit][offset : offset + size]

    omics = types.SimpleNamespace(
        SUBSOURCES=("geo", "sra"),
        search_subsource=search_subsource,
        PREFIXES=frozenset(),
        REQUIRES_EVERY_WORD=True,
    )
    hits = await _hits(monkeypatch, {"omics": omics})
    assert hits == {"geo:0": ["nitrogen"], "sra:0": ["nitrogen"]}


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_omicsdi_names_the_word_pxd055071_keeps_in_its_protocol() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client, query="Chlamydomonas nitrogen", sources=["omicsdi"], size=50
        )
    ids = {r.id: r for r in page.results}
    assert ids["omicsdi:pride:PXD055071"].unshown_terms == ["nitrogen"]
    # Positive control: a hit on the same page that says nitrogen claims nothing.
    assert ids["omicsdi:pride:PXD019491"].unshown_terms == []


# GEO's GSE165901 as the R2 runs saw it (2026-10-07): the first 500 characters are about
# frogs, and "single-cell RNA-seq" comes only after them.
_LEAD = (
    "Limb regeneration, while observed lifelong in salamanders, is restricted to "
    "pre-metamorphic stages in Xenopus laevis frogs. " * 6
)
_PAST_CUT = "Further, using single-cell RNA-seq analysis we find that the embryonic and adult"


def _geo(description: str):
    async def search(client, q, *, size=10, offset=0):
        hit = DataResource(
            id="geo:GSE1", source="geo", kind="study", title="Fibroblast dedifferentiation",
            description=description,
        )  # fmt: skip
        return 1, [compact(hit)][offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset(), REQUIRES_EVERY_WORD=True)


async def _page(monkeypatch, description: str, query: str):
    monkeypatch.setattr(router, "_ADAPTERS", {"omics": _geo(description)})
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query=query, sources=["omics"], size=10)
    return page.results[0]


async def test_a_hit_shows_where_its_cut_text_names_a_word_it_does_not_show(monkeypatch) -> None:
    hit = await _page(monkeypatch, _LEAD + _PAST_CUT, "salamanders single-cell")
    assert hit.unshown_terms == ["single cell"]
    assert hit.match_context is not None
    assert "using single-cell RNA-seq analysis" in hit.match_context
    assert len(hit.match_context) <= 250
    # The description still opens the record, as on every other hit.
    assert hit.description == (_LEAD + _PAST_CUT)[:500]


async def test_no_passage_when_every_word_shows_or_none_is_in_the_cut_text(monkeypatch) -> None:
    # Positive control: every word in the shown text, so nothing to point at.
    shown = await _page(monkeypatch, _LEAD + _PAST_CUT, "salamanders frogs")
    assert (shown.unshown_terms, shown.match_context) == ([], None)
    # A word the source matched some other way (a synonym, a field it does not send).
    elsewhere = await _page(monkeypatch, _LEAD + _PAST_CUT, "salamanders proteome")
    assert (elsewhere.unshown_terms, elsewhere.match_context) == (["proteome"], None)


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_geo_shows_the_single_cell_passage_gse165901_cuts() -> None:
    async with httpx.AsyncClient(timeout=90) as client:
        page = await router.search_page(
            client, query="axolotl blastema single-cell", sources=["omics"], size=30
        )
    ids = {r.id: r for r in page.results}
    hit = ids["geo:GSE165901"]
    assert hit.unshown_terms == ["single cell"]
    assert hit.match_context is not None
    assert "single-cell transcriptomic profiling" in hit.match_context
    # Positive control: a hit naming every word in what it shows points at nothing.
    shown = [r for r in page.results if not r.unshown_terms]
    assert shown
    assert all(r.match_context is None for r in shown)
