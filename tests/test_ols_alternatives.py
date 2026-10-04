"""A tissue or chemical name that is the label or a synonym of several ontology terms
(user decision 2026-10-04): the pick stays what it was, now by a stated rule (label
match, then the defining ontology, then OLS's order), and the other terms are echoed.

Live 2026-10-04 (OLS4, 40 common names): 15 were ambiguous, and OLS ranked the label
match, else the exact synonym, first in every one, so the old "defining, else first"
rule picked the ontology's term by OLS's ranking alone: "heart" is also a synonym of
"dorsal vessel heart", "skin" of "skin of body", "glucose" of "D-glucopyranose".
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _ols, _ontology, anatomy, chemistry

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _doc(obo_id: str, label: str, *synonyms: str, defining: bool = True) -> dict[str, Any]:
    return {
        "obo_id": obo_id,
        "label": label,
        "synonym": list(synonyms),
        "is_defining_ontology": defining,
    }


# OLS's live answer for "heart", in its order (2026-10-04)
_HEART = [
    _doc("UBERON:0000948", "heart", "vertebrate heart"),
    _doc("UBERON:0015230", "dorsal vessel heart", "heart"),
    _doc("UBERON:0015228", "circulatory organ", "heart"),
    _doc("UBERON:0007100", "primary circulatory organ", "heart"),
]


@pytest.fixture(autouse=True)
def _clear_caches():
    anatomy._CACHE.clear()
    chemistry._CACHE.clear()
    yield
    anatomy._CACHE.clear()
    chemistry._CACHE.clear()


def test_the_term_whose_label_is_the_name_wins_wherever_ols_ranks_it() -> None:
    reordered = [_HEART[1], _HEART[2], _HEART[0], _HEART[3]]
    info = anatomy._pick_uberon(reordered, "heart")
    assert info is not None and (info.uberon_id, info.canonical) == ("UBERON:0000948", "heart")
    assert [a.id for a in info.alternatives] == [
        "UBERON:0015230",
        "UBERON:0015228",
        "UBERON:0007100",
    ]
    # positive control: in OLS's own order the same term wins
    assert anatomy._pick_uberon(_HEART, "heart").uberon_id == "UBERON:0000948"


def test_a_label_match_wins_over_a_term_the_ontology_defines() -> None:
    docs = [
        _doc("CHEBI:4167", "D-glucopyranose", "glucose"),
        _doc("CHEBI:17234", "glucose", defining=False),
    ]
    info = chemistry._pick_chebi(docs, "glucose")
    assert info is not None and info.chebi_id == "CHEBI:17234"
    assert info.alternatives == (_ols.Alternative("CHEBI:4167", "D-glucopyranose"),)


def test_without_a_label_match_the_defined_term_then_ols_order_decides() -> None:
    imported = _doc("UBERON:0002097", "skin of body", "skin", defining=False)
    zone = _doc("UBERON:0000014", "zone of skin", "skin")
    info = anatomy._pick_uberon([imported, zone], "skin")
    assert info is not None and info.uberon_id == "UBERON:0000014"
    # both defined: OLS's order (it ranks UBERON's exact synonym first)
    both = [zone, _doc("UBERON:0002097", "skin of body", "skin")]
    info = anatomy._pick_uberon(both, "skin")
    assert info is not None and info.uberon_id == "UBERON:0000014"
    assert info.alternatives == (_ols.Alternative("UBERON:0002097", "skin of body"),)


def test_one_match_has_no_alternatives_and_non_matches_are_not_alternatives() -> None:
    docs = [_doc("UBERON:0002107", "liver", "iecur"), _doc("UBERON:0001114", "right lobe of liver")]
    info = anatomy._pick_uberon(docs, "liver")
    assert info is not None and info.alternatives == ()
    assert anatomy._pick_uberon(docs, "spleen") is None  # control: no match at all


def test_choose_ranks_label_then_defining_then_order() -> None:
    assert _ols.choose([(False, True), (True, False)]) == 1
    assert _ols.choose([(False, False), (False, True)]) == 1
    assert _ols.choose([(False, True), (False, True)]) == 0
    assert _ols.choose([(True, False), (True, True)]) == 1
    assert _ols.choose([(False, False)]) == 0


async def test_the_search_echoes_show_the_alternatives(monkeypatch) -> None:
    client = object()  # stands for the caller's httpx client
    answers = {
        ("heart", "uberon"): _HEART,
        ("glucose", "chebi"): [
            _doc("CHEBI:17234", "glucose"),
            _doc("CHEBI:4167", "D-glucopyranose", "glucose"),
        ],
        ("liver", "uberon"): [_doc("UBERON:0002107", "liver")],
    }

    async def exact_search(c, name, *, ontology, service):
        assert c is client
        return answers[(name, ontology)]

    monkeypatch.setattr(_ols, "exact_search", exact_search)
    errors: dict[str, str] = {}
    _q, tissue = await _ontology.expand_tissue(client, "rnaseq", "heart", errors)
    _q, chemical = await _ontology.expand_chemical(client, "rnaseq", "glucose", errors)
    assert errors == {}
    assert tissue is not None and tissue.uberon_id == "UBERON:0000948"
    assert [(a.id, a.label) for a in tissue.alternatives] == [
        ("UBERON:0015230", "dorsal vessel heart"),
        ("UBERON:0015228", "circulatory organ"),
        ("UBERON:0007100", "primary circulatory organ"),
    ]
    assert chemical is not None and chemical.chebi_id == "CHEBI:17234"
    assert [(a.id, a.label) for a in chemical.alternatives] == [("CHEBI:4167", "D-glucopyranose")]
    # positive control: an unambiguous name echoes none
    _q, liver = await _ontology.expand_tissue(client, "rnaseq", "liver", errors)
    assert liver is not None and liver.alternatives == []


@live_only
@pytest.mark.parametrize(
    ("ontology", "name", "chosen", "alternative"),
    [
        ("uberon", "skin", "UBERON:0000014", "UBERON:0002097"),
        ("uberon", "heart", "UBERON:0000948", "UBERON:0015230"),
        ("chebi", "glucose", "CHEBI:17234", "CHEBI:4167"),
        ("uberon", "liver", "UBERON:0002107", None),  # control: one match
    ],
)
async def test_live_ambiguous_names_keep_their_term_and_list_the_others(
    ontology: str, name: str, chosen: str, alternative: str | None
) -> None:
    resolve = anatomy.resolve_uberon if ontology == "uberon" else chemistry.resolve_chebi
    async with httpx.AsyncClient(timeout=30) as client:
        info = await resolve(client, name)
    assert info is not None
    picked = info.uberon_id if ontology == "uberon" else info.chebi_id
    assert picked == chosen
    ids = [a.id for a in info.alternatives]
    assert chosen not in ids
    if alternative is None:
        assert ids == []
    else:
        assert alternative in ids
