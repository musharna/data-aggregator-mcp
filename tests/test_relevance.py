"""What a hit names, as the default ranking reads it (``_relevance``)."""

from __future__ import annotations

from data_aggregator_mcp._ontology import FacetGroup
from data_aggregator_mcp._relevance import MatchTiers, query_terms
from data_aggregator_mcp.models import DataResource, Taxon

_ORGANISM = FacetGroup(
    input="Orobanche aegyptiaca", terms=("Phelipanche aegyptiaca", "Orobanche aegyptiaca")
)
_TISSUE = FacetGroup(input="liver", terms=("liver", "iecur"))


def _rec(title: str = "", **kw) -> DataResource:
    return DataResource(id="x:1", source="x", kind="dataset", title=title, **kw)


def test_query_terms_drop_operators_and_stop_words_and_keep_phrases() -> None:
    assert query_terms('"single cell" AND liver OR NOT the mouse-brain') == [
        "single cell",
        "liver",
        "mouse brain",
    ]
    # Lower-case "and" is a stop word, upper-case AND an operator; repeats count once.
    assert query_terms("drought and Drought (stress)") == ["drought", "stress"]
    assert query_terms("") == []


def test_a_facet_counts_once_whichever_of_its_names_a_hit_uses() -> None:
    tiers = MatchTiers("transcriptome", [_ORGANISM, _TISSUE])
    assert tiers.score(_rec("Phelipanche aegyptiaca transcriptome")) == (1, 1)
    assert tiers.score(_rec("Orobanche aegyptiaca and Phelipanche aegyptiaca")) == (1, 0)
    assert tiers.score(_rec("Orobanche aegyptiaca iecur transcriptomes")) == (2, 1)
    assert tiers.score(_rec("")) == (0, 0)


def test_names_are_whole_words_not_substrings() -> None:
    tiers = MatchTiers("rna", [_ORGANISM])
    # The live false hit: a cobra matched "aegyptia" upstream.
    assert tiers.score(_rec("first records of Walterinnesia aegyptia")) == (0, 0)
    assert tiers.score(_rec("RNase activity")) == (0, 0)
    # Positive controls: punctuation, case, hyphens and a plural "s" still match.
    assert tiers.score(_rec("(PHELIPANCHE AEGYPTIACA), RNA-seq")) == (1, 1)
    assert tiers.score(_rec("small RNAs")) == (0, 1)


def test_every_text_field_a_search_hit_carries_is_read() -> None:
    tiers = MatchTiers("drought", [_ORGANISM])
    hits = [
        _rec(description="Phelipanche aegyptiaca under drought"),
        _rec(subjects=["Phelipanche aegyptiaca", "drought"]),
        _rec(organism=["Phelipanche aegyptiaca"], title="drought"),
        _rec(taxa=[Taxon(taxid=1, name="Phelipanche aegyptiaca")], title="drought"),
    ]
    assert [tiers.score(h) for h in hits] == [(1, 1)] * 4


def test_no_query_and_no_facets_score_every_hit_the_same() -> None:
    tiers = MatchTiers("", [])
    assert {tiers.score(_rec(t)) for t in ("a", "liver", "")} == {(0, 0)}
