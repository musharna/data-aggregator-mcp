"""Rendering an ontology expansion for an upstream that caps its boolean operators."""

from __future__ import annotations

from data_aggregator_mcp import _ontology, literature, openaire
from data_aggregator_mcp._ontology import FacetGroup, count_operators, within_operator_limit

_SKIN = FacetGroup(
    input="skin",
    terms=(
        "zone of skin",
        "skin",
        "portion of skin",
        "region of skin",
        "skin region",
        "skin zone",
    ),
)
_MAIZE = FacetGroup(input="maize", terms=("Zea mays", "maize"))


def test_count_operators_counts_only_what_openaire_counts() -> None:
    # Counted: upper-case whole words outside quotes.
    assert count_operators("a AND b OR c NOT d") == 3
    assert count_operators('(x) AND ("a" OR "b")') == 2
    # Not counted (probed live): inside quotes, lower case, part of a word, symbols.
    assert count_operators('"rock AND roll" OR "salt and pepper"') == 1
    assert count_operators("androgen and ANDROGEN or ORCHID && NOTCH ||") == 0


def test_an_expansion_that_fits_is_returned_byte_identical() -> None:
    full = _ontology.and_group(_ontology.and_group("rna", _MAIZE.terms), _SKIN.terms)
    assert within_operator_limit("rna", [_MAIZE, _SKIN], 99) == (full, [])
    assert within_operator_limit("rna", [_MAIZE, _SKIN], count_operators(full)) == (full, [])
    assert within_operator_limit("rna", [], 0) == ("rna", [])


def test_a_long_facet_keeps_the_typed_name_first_and_lists_what_it_left_out() -> None:
    query, left_out = within_operator_limit("single-cell", [_SKIN], 4)  # type: ignore[misc]
    assert query == (
        '(single-cell) AND ("skin" OR "zone of skin" OR "portion of skin" OR "region of skin")'
    )
    assert left_out == ["skin region", "skin zone"]
    assert count_operators(query) == 4


def test_spare_slots_go_round_robin_and_skip_a_full_facet() -> None:
    # limit 5, two ANDs: three spare ORs. maize takes one (then is full), skin takes two.
    query, left_out = within_operator_limit("rna", [_MAIZE, _SKIN], 5)  # type: ignore[misc]
    assert query == (
        '((rna) AND ("maize" OR "Zea mays")) AND ("skin" OR "zone of skin" OR "portion of skin")'
    )
    assert left_out == ["region of skin", "skin region", "skin zone"]
    # limit 3: one spare OR, so the first facet gets it and skin keeps its typed name only.
    query, left_out = within_operator_limit("rna", [_MAIZE, _SKIN], 3)  # type: ignore[misc]
    assert query == '((rna) AND ("maize" OR "Zea mays")) AND ("skin")'
    assert left_out == list(_SKIN.terms[:1] + _SKIN.terms[2:])


def test_none_when_not_even_one_name_per_facet_fits() -> None:
    # The caller's own query already spends 3; two facets need 2 more ANDs.
    assert within_operator_limit("a AND b AND c AND d", [_MAIZE, _SKIN], 4) is None
    # Positive control: one more slot and each facet keeps its typed name.
    assert within_operator_limit("a AND b AND c AND d", [_MAIZE, _SKIN], 5) == (
        '((a AND b AND c AND d) AND ("maize")) AND ("skin")',
        ["Zea mays", *_SKIN.terms[:1], *_SKIN.terms[2:]],
    )


def test_every_rendering_fits_and_keeps_every_facet() -> None:
    for limit in range(2, 12):
        fitted = within_operator_limit("rna", [_MAIZE, _SKIN], limit)
        assert fitted is not None
        query, left_out = fitted
        assert count_operators(query) <= limit
        assert '"maize"' in query and '"skin"' in query
        kept = [t for g in (_MAIZE, _SKIN) for t in g.terms if f'"{t}"' in query]
        assert sorted(kept + left_out) == sorted(_MAIZE.terms + _SKIN.terms)


def test_the_typed_name_matches_ignoring_case_and_an_unlisted_one_keeps_the_order() -> None:
    group = FacetGroup(input="breast cancer", terms=("Breast Neoplasms", "x", " Breast Cancer"))
    assert within_operator_limit("q", [group], 2) == (
        '(q) AND ("Breast Cancer" OR "Breast Neoplasms")',
        ["x"],
    )
    group = FacetGroup(input="kidney", terms=("renal organ", "rein", "nephros"))
    assert within_operator_limit("q", [group], 2) == (
        '(q) AND ("renal organ" OR "rein")',
        ["nephros"],
    )


def test_openaire_declares_its_limit_through_literature() -> None:
    assert openaire.MAX_OPERATORS == 4
    assert literature.OPERATOR_LIMITS == {"openaire": 4}
