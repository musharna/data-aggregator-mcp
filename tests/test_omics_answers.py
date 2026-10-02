"""What the omics adapter makes of NCBI's answers, beside the live shapes they came from.

Probed live 2026-10-02 (esummary version 2.0): 600 gds summaries (GSE/GSM/GPL/GDS), 400 sra
and 400 bioproject summaries from varied queries. Every field the adapter reads was a
string in all 1,400, and 11 of the 600 gds ``taxon`` values named several organisms.
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import omics, router, taxonomy
from data_aggregator_mcp.taxonomy import TaxonInfo

live_only = pytest.mark.skipif(
    os.environ.get("DATA_AGGREGATOR_MCP_LIVE") != "1",
    reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run",
)

# Verbatim live gds taxon values (GSE347972, GDS5814, GSE294673).
_MULTI_TAXON = {
    "Homo sapiens; Mus musculus": ["Homo sapiens", "Mus musculus"],
    "Schizosaccharomyces pombe; Saccharomyces cerevisiae": [
        "Schizosaccharomyces pombe",
        "Saccharomyces cerevisiae",
    ],
    "Homo sapiens; Drosophila melanogaster; Mus musculus": [
        "Homo sapiens",
        "Drosophila melanogaster",
        "Mus musculus",
    ],
}


def test_a_geo_entry_of_several_organisms_lists_each_one() -> None:
    """GEO joins an entry's organisms with "; ". Read as one name, "Homo sapiens; Mus
    musculus" was one organism, and the taxonomy lookup (which NCBI answers with the one
    phrase it can match, here Mus musculus) dropped the other. Positive control: a
    single-organism entry and an entry with no taxon read as before."""
    for taxon, names in _MULTI_TAXON.items():
        r = omics._normalize_geo({"accession": "GSE1", "title": "t", "taxon": taxon})
        assert r.organism == names, taxon
    one = omics._normalize_geo({"accession": "GSE2", "title": "t", "taxon": "Arabidopsis thaliana"})
    assert one.organism == ["Arabidopsis thaliana"]
    assert omics._normalize_geo({"accession": "GSE3", "title": "t", "taxon": ""}).organism == []


async def test_resolve_looks_up_every_organism_of_a_geo_entry(monkeypatch) -> None:
    """End to end through the router: each organism of a two-organism series reaches the
    taxonomy lookup and becomes a taxon of the record."""
    asked: list[str] = []
    known = {"Homo sapiens": 9606, "Mus musculus": 10090}

    async def fake_resolve_taxon(client, name):
        asked.append(name)
        taxid = known.get(name)
        return None if taxid is None else TaxonInfo(taxid, name, (), False)

    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        return 1, ["200347972"]

    async def fake_esummary(client, db, ids):
        doc = {"uid": "200347972", "accession": "GSE347972", "title": "t"}
        return [{**doc, "taxon": "Homo sapiens; Mus musculus", "ftplink": ""}]

    async def no_files(client, ftplink):
        return []

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve_taxon)
    monkeypatch.setattr(omics._eutils, "esearch", fake_esearch)
    monkeypatch.setattr(omics._eutils, "esummary", fake_esummary)
    monkeypatch.setattr(omics.geo, "supplementary_files", no_files)
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GSE347972")
    router._RESOLVE_CACHE.clear()
    assert asked == ["Homo sapiens", "Mus musculus"]
    assert [(t.taxid, t.name) for t in r.taxa] == [(9606, "Homo sapiens"), (10090, "Mus musculus")]


@live_only
async def test_live_a_two_organism_geo_dataset_resolves_with_both_taxa() -> None:
    """GDS5814 (a frozen GEO DataSet) is fission and budding yeast; resolved through the
    router, both organisms are listed and both become taxa."""
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GDS5814")
    router._RESOLVE_CACHE.clear()
    assert r.organism == ["Schizosaccharomyces pombe", "Saccharomyces cerevisiae"]
    assert {t.taxid for t in r.taxa} == {4896, 4932}
