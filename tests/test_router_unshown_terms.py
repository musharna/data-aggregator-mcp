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
from data_aggregator_mcp.models import DataResource

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
