"""An organism name that matches several NCBI taxa (user decision 2026-10-04).

esearch db=taxonomy lists the taxa a name matches by descending taxid, and
``resolve_taxon`` took the first: "Drosophila" resolved to a fungus genus, "fruit fly"
to *Drosophila gunungcola*, "bacteria" to a stick-insect genus. The candidates and
record counts below are NCBI's live answers (2026-10-04): the taxa esearch lists for the
name, and the nuccore esearch count of ``txid<N>[Organism:noexp]`` (records annotated to
the taxon itself) and ``[Organism:exp]`` (with its descendants).
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from data_aggregator_mcp import _ontology, taxonomy

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# taxid -> (scientific name, noexp count, exp count)
_TAXA = {
    2081351: ("Drosophila", 0, 4),  # the fungus genus
    32281: ("Drosophila", 0, 409_797),  # the fly subgenus
    7215: ("Drosophila", 45, 3_375_999),  # the fly genus
    103775: ("Drosophila gunungcola", 27_595, 27_595),
    7227: ("Drosophila melanogaster", 1_341_557, 1_341_557),
    7211: ("Tephritidae", 0, 810_204),
    10090: ("Mus musculus", 10_596_556, 11_207_660),
    10088: ("Mus", 2_375, 11_349_589),
    381124: ("Zea mays subsp. mays", 529_900, 529_900),
    4577: ("Zea mays", 4_335_445, 4_890_239),
    3702: ("Arabidopsis thaliana", 1, 1),
}
# name -> the taxids esearch lists for it, in NCBI's order
_NAMES = {
    "drosophila": [2081351, 32281, 7215],
    "fruit fly": [103775, 7227, 7215, 7211],
    "mouse": [10090, 10088],
    "maize": [381124, 4577],
    "arabidopsis thaliana": [3702],
}


class _NCBI:
    """esearch/efetch answering from copies of the tables above (a test may edit its own);
    records each nuccore count asked."""

    def __init__(self) -> None:
        self.taxa, self.names = dict(_TAXA), {k: list(v) for k, v in _NAMES.items()}
        self.counted: list[str] = []

    async def esearch(self, client, db, term, *, retmax, retstart=0):
        if db == "taxonomy":
            ids = self.names.get(term.strip().lower(), [])
            return len(ids), [str(i) for i in ids[:retmax]]
        assert db == "nuccore" and retmax == 0, (db, retmax)
        self.counted.append(term)
        m = re.fullmatch(r"txid(\d+)\[Organism:(noexp|exp)\]", term)
        assert m, term
        _name, own, wide = self.taxa[int(m[1])]
        return (own if m[2] == "noexp" else wide), []

    async def efetch(self, client, db, ids, retmode="xml"):
        assert db == "taxonomy"
        body = "".join(
            f"<Taxon><TaxId>{i}</TaxId><ScientificName>{self.taxa[int(i)][0]}</ScientificName>"
            "<Lineage>cellular organisms; Eukaryota</Lineage></Taxon>"
            for i in ids
        )
        return f"<TaxaSet>{body}</TaxaSet>"


@pytest.fixture
def ncbi(monkeypatch) -> _NCBI:
    fake = _NCBI()
    monkeypatch.setattr(taxonomy._eutils, "esearch", fake.esearch)
    monkeypatch.setattr(taxonomy._eutils, "efetch", fake.efetch)
    taxonomy._CACHE.clear()
    yield fake
    taxonomy._CACHE.clear()


@pytest.mark.parametrize(
    ("name", "taxid", "why"),
    [
        ("Drosophila", 7215, "the fly genus, not the fungus genus NCBI lists first"),
        ("fruit fly", 7227, "D. melanogaster, not D. gunungcola"),
        ("mouse", 10090, "the house mouse: the genus Mus wins only counting descendants"),
        ("maize", 4577, "Zea mays, not its subspecies"),
    ],
)
async def test_an_ambiguous_name_resolves_to_the_best_known_taxon(
    ncbi: _NCBI, name: str, taxid: int, why: str
) -> None:
    info = await taxonomy.resolve_taxon(None, name)
    assert info is not None and info.taxid == taxid, why
    assert info.canonical_name == _TAXA[taxid][0]


async def test_the_other_candidates_are_reported_in_ncbi_order(ncbi: _NCBI) -> None:
    info = await taxonomy.resolve_taxon(None, "fruit fly")
    assert info is not None and info.taxid == 7227
    assert [(a.taxid, a.name) for a in info.alternatives] == [
        (103775, "Drosophila gunungcola"),
        (7215, "Drosophila"),
        (7211, "Tephritidae"),
    ]
    # positive control: a name matching one taxon has none, and costs no record counts
    only = await taxonomy.resolve_taxon(None, "Arabidopsis thaliana")
    assert only is not None and only.taxid == 3702 and only.alternatives == ()
    assert not any("3702" in term for term in ncbi.counted)


async def test_an_exact_scientific_name_wins_without_counting_records(ncbi: _NCBI) -> None:
    """ "Zea mays" names taxon 4577 exactly; its subspecies, also listed, is not chosen
    even if it had more records, and no records are counted."""
    ncbi.names["zea mays"] = [381124, 4577]
    ncbi.taxa[381124] = ("Zea mays subsp. mays", 9_999_999_999, 9_999_999_999)
    info = await taxonomy.resolve_taxon(None, "zea mays")
    assert info is not None and info.taxid == 4577
    assert [a.taxid for a in info.alternatives] == [381124]
    assert ncbi.counted == []
    # positive control: without an exact match the counts decide, and the subspecies wins
    info = await taxonomy.resolve_taxon(None, "maize")
    assert info is not None and info.taxid == 381124
    assert ncbi.counted == ["txid381124[Organism:noexp]", "txid4577[Organism:noexp]"]


async def test_homonyms_are_ranked_by_their_own_records(ncbi: _NCBI) -> None:
    """All three "Drosophila" taxa match the name exactly, so the counts rank them; the
    taxon's own records decide (45 vs 0 vs 0), and no descendant count is needed."""
    info = await taxonomy.resolve_taxon(None, "Drosophila")
    assert info is not None and info.taxid == 7215
    assert all(term.endswith("[Organism:noexp]") for term in ncbi.counted)
    assert len(ncbi.counted) == 3


async def test_a_tie_on_own_records_goes_to_descendants_then_the_smaller_taxid(
    ncbi: _NCBI,
) -> None:
    ncbi.names["tied"] = [32281, 2081351]  # both "Drosophila", 0 own records each
    info = await taxonomy.resolve_taxon(None, "tied")
    assert info is not None and info.taxid == 32281  # 409,797 with descendants vs 4
    ncbi.taxa[32281] = ("Drosophila", 0, 4)
    ncbi.names["tied again"] = [32281, 2081351]
    info = await taxonomy.resolve_taxon(None, "tied again")
    assert info is not None and info.taxid == 32281  # 4 vs 4: the smaller taxid
    ncbi.names["tied once more"] = [2081351, 32281]  # the order NCBI lists does not decide
    info = await taxonomy.resolve_taxon(None, "tied once more")
    assert info is not None and info.taxid == 32281


async def test_the_chosen_taxon_is_cached_with_its_alternatives(ncbi: _NCBI) -> None:
    first = await taxonomy.resolve_taxon(None, "mouse")
    asked = len(ncbi.counted)
    again = await taxonomy.resolve_taxon(None, " MOUSE ")
    assert again is first and len(ncbi.counted) == asked == 2
    assert [a.taxid for a in again.alternatives] == [10088]


async def test_the_search_echo_shows_the_alternatives(ncbi: _NCBI) -> None:
    errors: dict[str, str] = {}
    query, echo = await _ontology.expand_organism(None, "rnaseq", "Drosophila", errors)
    assert errors == {}
    assert echo is not None and echo.taxid == 7215
    assert [(a.taxid, a.name) for a in echo.alternatives] == [
        (2081351, "Drosophila"),
        (32281, "Drosophila"),
    ]
    assert query == '(rnaseq) AND ("Drosophila")'
    # positive control: an unambiguous name echoes no alternatives
    _query, echo = await _ontology.expand_organism(None, "rnaseq", "Arabidopsis thaliana", errors)
    assert echo is not None and echo.alternatives == []


@live_only
@pytest.mark.parametrize(
    ("name", "taxid", "alternative"),
    [
        ("Drosophila", 7215, 2081351),
        ("fruit fly", 7227, 103775),
        ("mouse", 10090, 10088),
        ("bacteria", 2, None),
        ("Arabidopsis thaliana", 3702, None),  # control: one candidate
    ],
)
async def test_live_ambiguous_names_resolve_to_the_best_known_taxon(
    name: str, taxid: int, alternative: int | None
) -> None:
    taxonomy._CACHE.clear()
    async with httpx.AsyncClient(timeout=30) as client:
        info = await taxonomy.resolve_taxon(client, name)
    assert info is not None and info.taxid == taxid
    alternatives = [a.taxid for a in info.alternatives]
    if name == "Arabidopsis thaliana":
        assert alternatives == []
    else:
        assert alternatives and taxid not in alternatives
    if alternative is not None:
        assert alternative in alternatives
