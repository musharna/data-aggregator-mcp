"""What a hit names, as the default ranking reads it (``_relevance``)."""

from __future__ import annotations

from data_aggregator_mcp._ontology import FacetGroup
from data_aggregator_mcp._relevance import MatchTiers, passage, query_phrase, query_terms
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


def test_unshown_lists_the_query_terms_a_hit_does_not_name_in_query_order() -> None:
    tiers = MatchTiers('Chlamydomonas "nitrogen starvation" proteomics', [])
    hit = _rec("Extracellular vesicles in Chlamydomonas", description="A proteomics study.")
    assert tiers.unshown(hit) == ["nitrogen starvation"]
    # Every field the ranking reads counts; "proteomic" does not name "proteomics".
    assert tiers.unshown(_rec("x", subjects=["nitrogen starvation"], description="proteomic")) == [
        "chlamydomonas",
        "proteomics",
    ]
    # Positive control: a hit naming every term (one as a plural) leaves none, and so does
    # an empty query.
    assert tiers.unshown(_rec("Chlamydomonas proteomics under nitrogen starvations")) == []
    assert MatchTiers("", []).unshown(hit) == []


_PROTOCOL = (
    "Cells were grown in TAP medium to mid-log phase and harvested by centrifugation. "
    "Subcellular fractions were then prepared from N-starved cultures after 48 h of "
    "nitrogen depletion, and lipid droplets were isolated on a sucrose gradient before "
    "digestion with trypsin and analysis on an Orbitrap mass spectrometer."
)


def test_passage_is_the_stretch_around_the_first_term_the_text_names() -> None:
    got = passage(_PROTOCOL, ["proteome", "nitrogen depletion"], width=80)
    assert got is not None
    assert "nitrogen depletion" in got
    # Cut at spaces, with an ellipsis on each cut side; within the width plus the marks.
    assert got.startswith("…") and got.endswith("…")
    inner = got[1:-1]
    assert inner in _PROTOCOL
    at = _PROTOCOL.index(inner)
    assert _PROTOCOL[at - 1] == " " and _PROTOCOL[at + len(inner)] == " "
    assert len(got) <= 82
    # Text before the match is kept, so the reader sees what it is about.
    assert got.index("nitrogen") > 5
    # The first place named wins, whichever term names it.
    assert "Subcellular" in (passage(_PROTOCOL, ["lipid droplet", "subcellular"], width=60) or "")


def test_passage_reads_terms_as_whole_words_with_a_plural() -> None:
    # "cell" is named by "Cells", never inside "Subcellular".
    first = passage(_PROTOCOL, ["cell"], width=40)
    assert first is not None and first.startswith("Cells were")
    assert passage(_PROTOCOL, ["cellular", "sub"]) is None
    # Punctuation between a term's words: "n starved" is named by "N-starved".
    assert "N-starved" in (passage(_PROTOCOL, ["n starved"], width=60) or "")
    # A term the text does not name, and an empty term list: nothing to show.
    assert passage(_PROTOCOL, ["proteome"]) is None
    assert passage(_PROTOCOL, []) is None
    assert passage(_PROTOCOL, ["droplet"], width=len(_PROTOCOL) + 10) == _PROTOCOL
