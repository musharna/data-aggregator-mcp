"""What a hit names, as the default ranking reads it (``_relevance``)."""

from __future__ import annotations

from data_aggregator_mcp._ontology import FacetGroup
from data_aggregator_mcp._relevance import MatchTiers, query_phrase, query_terms
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
    assert tiers.score(_rec("Phelipanche aegyptiaca transcriptome")) == (1, 1, 1, 1)
    assert tiers.score(_rec("Orobanche aegyptiaca and Phelipanche aegyptiaca")) == (1, 0, 0, 0)
    assert tiers.score(_rec("Orobanche aegyptiaca iecur transcriptomes")) == (2, 1, 1, 1)
    assert tiers.score(_rec("")) == (0, 0, 0, 0)


def test_names_are_whole_words_not_substrings() -> None:
    tiers = MatchTiers("rna", [_ORGANISM])
    # The live false hit: a cobra matched "aegyptia" upstream.
    assert tiers.score(_rec("first records of Walterinnesia aegyptia")) == (0, 0, 0, 0)
    assert tiers.score(_rec("RNase activity")) == (0, 0, 0, 0)
    # Positive controls: punctuation, case, hyphens and a plural "s" still match.
    assert tiers.score(_rec("(PHELIPANCHE AEGYPTIACA), RNA-seq")) == (1, 1, 1, 1)
    assert tiers.score(_rec("small RNAs")) == (0, 1, 1, 1)


def test_every_text_field_a_search_hit_carries_is_read() -> None:
    tiers = MatchTiers("drought", [_ORGANISM])
    hits = [
        _rec(description="Phelipanche aegyptiaca under drought"),
        _rec(subjects=["Phelipanche aegyptiaca", "drought"]),
        _rec(organism=["Phelipanche aegyptiaca"], title="drought"),
        _rec(taxa=[Taxon(taxid=1, name="Phelipanche aegyptiaca")], title="drought"),
    ]
    # the title tier reads the title alone; the others read every field
    assert [tiers.score(h) for h in hits] == [
        (1, 0, 1, 1),
        (1, 0, 1, 1),
        (1, 1, 1, 1),
        (1, 1, 1, 1),
    ]


def test_no_query_and_no_facets_score_every_hit_the_same() -> None:
    tiers = MatchTiers("", [])
    assert {tiers.score(_rec(t)) for t in ("a", "liver", "")} == {(0, 0, 0, 0)}


def test_the_query_as_written_outranks_its_words_scattered() -> None:
    """The live tie: Antarctic logs name "snow" and "leopard" (seals), a museum's whole
    collection names snow leopard in its description, a study names it in its title."""
    tiers = MatchTiers("snow leopard", [])
    log = _rec(
        "Log of biological observations at Casey, 1974",
        description="Snow petrels nested near the hut; leopard seals hauled out on the ice.",
    )
    collection = _rec(
        "Mammalogy collection", description="Specimens include snow leopards and wolves."
    )
    study = _rec("Prey preference of snow leopard (Panthera uncia) in South Gobi")
    assert tiers.score(log) == (0, 0, 0, 2)
    assert tiers.score(collection) == (0, 0, 1, 2)  # plural "s" still names it
    assert tiers.score(study) == (0, 1, 1, 2)
    ranked = sorted([log, collection, study], key=tiers.score, reverse=True)
    assert ranked == [study, collection, log]


def test_a_facet_still_outranks_the_phrase() -> None:
    tiers = MatchTiers(
        "snow leopard", [FacetGroup(input="Panthera uncia", terms=("Panthera uncia",))]
    )
    named = _rec("Camera traps in Mongolia", organism=["Panthera uncia"])
    phrase_only = _rec("Snow leopard tourism survey")
    assert tiers.score(named) > tiers.score(phrase_only)


def test_query_phrase_keeps_stop_words_and_drops_operators_and_quotes() -> None:
    assert (
        query_phrase('Seals of Antarctica AND "Weddell sea"') == "seals of antarctica weddell sea"
    )
    assert query_phrase("snow-leopard") == "snow leopard"
    assert query_phrase("") == ""
