"""The dossier's assessment entities, pinned whole (#88 mutation burn-down of `dossier`).

`tests/test_dossier.py` checks that each signal is present or omitted and reads a few
fields; these tests compare every entity with a literal written out by hand, so a
garbled ``@type``, ``name`` or human-readable ``value``, or a dropped field, fails.
Expected values are never rebuilt with the code's own formulas.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from data_aggregator_mcp import dossier
from data_aggregator_mcp.models import FairAssessment, Link, TrustSignals
from tests.test_dossier import NOW, _graph, _resource


def _entity(eid: str, **over: Any) -> dict[str, Any]:
    return _graph(dossier.render(_resource(**over), now=NOW))[eid]


# --- version-currency -------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        (
            {"is_latest": True},
            {
                "@id": "#version-currency",
                "@type": "PropertyValue",
                "name": "version-currency",
                "value": "this record is the latest known version",
                "is_latest": True,
            },
        ),
        (
            {"is_latest": False, "superseded_by": "zenodo:9"},
            {
                "@id": "#version-currency",
                "@type": "PropertyValue",
                "name": "version-currency",
                "value": "this record is superseded by a newer version: zenodo:9",
                "is_latest": False,
                "superseded_by": "zenodo:9",
            },
        ),
        (
            {"is_latest": False, "superseded_by": None},
            {
                "@id": "#version-currency",
                "@type": "PropertyValue",
                "name": "version-currency",
                "value": "this record is superseded by a newer version",
                "is_latest": False,
            },
        ),
    ],
    ids=["latest", "superseded-by-known-id", "superseded-by-unknown"],
)
def test_version_currency_entity_is_exact(over: dict[str, Any], expected: dict[str, Any]) -> None:
    assert _entity("#version-currency", **over) == expected


# --- licence ----------------------------------------------------------------


def test_licence_entity_is_exact_for_a_recognised_and_an_unrecognised_licence() -> None:
    assert _entity("#licence", license="cc-by-4.0") == {
        "@id": "#licence",
        "@type": "PropertyValue",
        "name": "licence",
        "value": "stated licence 'cc-by-4.0'; normalized SPDX: CC-BY-4.0",
        "license_raw": "cc-by-4.0",
        "normalized_spdx": "CC-BY-4.0",
    }
    assert _entity("#licence", license="see paper") == {
        "@id": "#licence",
        "@type": "PropertyValue",
        "name": "licence",
        "value": "stated licence 'see paper'; normalized SPDX: unrecognized",
        "license_raw": "see paper",
        "normalized_spdx": None,
    }


# --- FAIR -------------------------------------------------------------------


def test_fair_entity_is_exact() -> None:
    fa = FairAssessment(
        score=72,
        findable=80,
        accessible=60,
        interoperable=70,
        reusable=78,
        assessed=12,
        gaps=["metadata does not expose X (RDA-F1-01D)"],
    )
    assert _entity("#fair", fair=fa) == {
        "@id": "#fair",
        "@type": "PropertyValue",
        "name": "FAIRness",
        "value": "FAIR score 72/100 (F=80 A=60 I=70 R=78); 12 indicators evaluated",
        "score": 72,
        "findable": 80,
        "accessible": 60,
        "interoperable": 70,
        "reusable": 78,
        "assessed": 12,
        "gaps": ["metadata does not expose X (RDA-F1-01D)"],
    }


# --- retraction / expression of concern ---------------------------------------

_RETR_UNKNOWN = "retraction status unknown / not checked"
_RETR_TRUE = "RETRACTED: a retraction is on record (Crossref)"
_RETR_FALSE = "no retraction on record (Crossref)"
_CONC_UNKNOWN = "expression-of-concern status unknown / not checked"
_CONC_TRUE = "an expression of concern is on record (Crossref)"
_CONC_FALSE = "no expression of concern on record (Crossref)"


@pytest.mark.parametrize(
    ("retracted", "concern", "value"),
    [
        (None, None, f"{_RETR_UNKNOWN}; {_CONC_UNKNOWN}"),
        (None, True, f"{_RETR_UNKNOWN}; {_CONC_TRUE}"),
        (None, False, f"{_RETR_UNKNOWN}; {_CONC_FALSE}"),
        (True, None, f"{_RETR_TRUE}; {_CONC_UNKNOWN}"),
        (True, True, f"{_RETR_TRUE}; {_CONC_TRUE}"),
        (True, False, f"{_RETR_TRUE}; {_CONC_FALSE}"),
        (False, None, f"{_RETR_FALSE}; {_CONC_UNKNOWN}"),
        (False, True, f"{_RETR_FALSE}; {_CONC_TRUE}"),
        (False, False, f"{_RETR_FALSE}; {_CONC_FALSE}"),
    ],
)
def test_retraction_entity_is_exact_for_every_signal_pair(
    retracted: bool | None, concern: bool | None, value: str
) -> None:
    trust = TrustSignals(retracted=retracted, concern=concern)
    assert _entity("#retraction", trust=trust) == {
        "@id": "#retraction",
        "@type": "PropertyValue",
        "name": "retraction-status",
        "value": value,
        "retracted": retracted,
        "concern": concern,
    }


def test_a_retraction_notice_doi_is_named_in_the_value_and_carried_as_a_field() -> None:
    trust = TrustSignals(retracted=True, retraction_doi="10.1/retraction", concern=False)
    assert _entity("#retraction", trust=trust) == {
        "@id": "#retraction",
        "@type": "PropertyValue",
        "name": "retraction-status",
        "value": (
            "RETRACTED: a retraction is on record (Crossref); retraction notice "
            "10.1/retraction; no expression of concern on record (Crossref)"
        ),
        "retracted": True,
        "concern": False,
        "retraction_doi": "10.1/retraction",
    }


# --- source / DOI / ID chain --------------------------------------------------


def test_identifier_chain_entity_is_exact_with_and_without_optional_ids() -> None:
    assert _entity("#identifier-chain", doi=None) == {
        "@id": "#identifier-chain",
        "@type": "PropertyValue",
        "name": "source-identifier-chain",
        "value": "source repository zenodo; canonical id zenodo:1",
        "source": "zenodo",
        "canonical_id": "zenodo:1",
    }
    assert _entity(
        "#identifier-chain",
        identifiers={"pmid": "12345"},
        accessions=["GSE1"],
        links=[Link(rel="is_supplement_to", target_id="pmid:12345")],
    ) == {
        "@id": "#identifier-chain",
        "@type": "PropertyValue",
        "name": "source-identifier-chain",
        "value": "source repository zenodo; canonical id zenodo:1; DOI 10.5281/zenodo.1",
        "source": "zenodo",
        "canonical_id": "zenodo:1",
        "doi": "10.5281/zenodo.1",
        "identifiers": [{"@id": "#identifier-0"}],
        "accessions": ["GSE1"],
        "links": [{"@id": "#link-0"}],
    }


# --- the action's results, in order -------------------------------------------


def test_the_action_lists_every_present_signal_in_fixed_order() -> None:
    full = _graph(
        dossier.render(
            _resource(
                is_latest=True,
                fair=FairAssessment(
                    score=1,
                    findable=1,
                    accessible=1,
                    interoperable=1,
                    reusable=1,
                    assessed=1,
                ),
                trust=TrustSignals(),
            ),
            now=NOW,
        )
    )
    assert full["#provenance-assessment"]["result"] == [
        {"@id": "#version-currency"},
        {"@id": "#licence"},
        {"@id": "#fair"},
        {"@id": "#retraction"},
        {"@id": "#identifier-chain"},
    ]
    bare = _graph(dossier.render(_resource(license=None), now=NOW))
    assert bare["#provenance-assessment"]["result"] == [{"@id": "#identifier-chain"}]


# --- live: the real resolve(format=provenance) path ---------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_provenance_resolve_reports_definitive_crossref_answers() -> None:
    """Drive the tool handler end to end (PDB adapter, Crossref trust lookups, FAIR,
    dossier) for haemoglobin 4HHB, whose own DOI and 1984 paper are both Crossref works
    with no retraction or concern: the dossier states that, and names the chain."""
    from data_aggregator_mcp import server

    out = await server._dispatch("resolve", {"id": "pdb:4HHB", "format": "provenance"})
    g = _graph(out["provenance"])
    assert g["#retraction"] == {
        "@id": "#retraction",
        "@type": "PropertyValue",
        "name": "retraction-status",
        "value": "no retraction on record (Crossref); no expression of concern on record (Crossref)",
        "retracted": False,
        "concern": False,
    }
    chain = g["#identifier-chain"]
    assert chain["value"] == (
        "source repository pdb; canonical id pdb:4HHB; DOI 10.2210/pdb4hhb/pdb"
    )
    assert {"rel": "described_in", "target_id": "10.1016/0022-2836(84)90472-8"} in chain["links"]
    assert g["#provenance-assessment"]["result"] == [
        {"@id": "#fair"},
        {"@id": "#retraction"},
        {"@id": "#identifier-chain"},
    ]
