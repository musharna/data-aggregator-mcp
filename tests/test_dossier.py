"""Tests for the RO-Crate 1.1 provenance data-availability dossier (B10a)."""

from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

from data_aggregator_mcp import __version__, dossier
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FairAssessment,
    FileEntry,
    Link,
    TrustSignals,
)


def _resource(**over: Any) -> DataResource:
    base = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="Rice genomes",
        description="d",
        doi="10.5281/zenodo.1",
        creators=[Creator(name="A. Author")],
        year=2024,
        license="cc-by-4.0",
        files=[FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=10)],
    )
    return base.model_copy(update=over)


def _graph(crate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    graph: list[dict[str, Any]] = crate["@graph"]
    return {e["@id"]: e for e in graph}


# --- purity / determinism ---------------------------------------------------


def test_render_signature_takes_only_resource() -> None:
    params = list(inspect.signature(dossier.render).parameters)
    assert params == ["resource"]


def test_render_is_deterministic() -> None:
    r = _resource()
    assert dossier.render(r) == dossier.render(r)


def test_render_does_not_mutate_resource() -> None:
    r = _resource()
    before = r.model_dump()
    dossier.render(r)
    assert r.model_dump() == before


# --- structural RO-Crate 1.1 validity --------------------------------------


def test_reuses_ro_crate_base() -> None:
    crate = dossier.render(_resource())
    assert crate["@context"] == "https://w3id.org/ro/crate/1.1/context"
    g = _graph(crate)
    assert g["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    assert g["./"]["@type"] == "Dataset"
    # File entity from the base crate survives.
    assert g["https://x/a.csv"]["@type"] == "File"


def test_conforms_to_only_on_metadata_descriptor() -> None:
    """No fabricated profile URI: conformsTo appears only on ro-crate-metadata.json."""
    crate = dossier.render(_resource())
    carriers = [e["@id"] for e in crate["@graph"] if "conformsTo" in e]
    assert carriers == ["ro-crate-metadata.json"]


def test_create_action_present_with_instrument_object_result() -> None:
    crate = dossier.render(_resource())
    g = _graph(crate)
    action = g["#provenance-assessment"]
    assert action["@type"] == "CreateAction"
    assert action["name"] == "data-aggregator-mcp provenance assessment"
    assert action["instrument"] == {"@id": dossier.AGENT_ID}
    assert action["object"] == {"@id": "./"}
    assert isinstance(action["result"], list)
    # Every result reference resolves to an entity in the graph.
    for ref in action["result"]:
        assert ref["@id"] in g


def test_agent_carries_version() -> None:
    crate = dossier.render(_resource())
    g = _graph(crate)
    agent = g[dossier.AGENT_ID]
    assert agent["@type"] == "SoftwareApplication"
    assert agent["name"] == "data-aggregator-mcp"
    assert agent["version"] == __version__


def test_root_mentions_assessment() -> None:
    crate = dossier.render(_resource())
    g = _graph(crate)
    assert g["./"]["mentions"] == {"@id": "#provenance-assessment"}


def test_end_time_present_only_with_last_updated() -> None:
    with_ts = _graph(dossier.render(_resource(last_updated="2024-01-02T00:00:00Z")))
    assert with_ts["#provenance-assessment"]["endTime"] == "2024-01-02T00:00:00Z"
    without = _graph(dossier.render(_resource(last_updated=None)))
    assert "endTime" not in without["#provenance-assessment"]


# --- composition: each signal present only when present --------------------


def test_version_currency_names_superseding_id() -> None:
    g = _graph(dossier.render(_resource(is_latest=False, superseded_by="zenodo:9")))
    ver = g["#version-currency"]
    assert ver["is_latest"] is False
    assert ver["superseded_by"] == "zenodo:9"
    assert "zenodo:9" in ver["value"]


def test_version_currency_latest() -> None:
    g = _graph(dossier.render(_resource(is_latest=True)))
    ver = g["#version-currency"]
    assert ver["is_latest"] is True
    assert "latest" in ver["value"].lower()


def test_version_currency_omitted_when_unknown() -> None:
    g = _graph(dossier.render(_resource(is_latest=None)))
    assert "#version-currency" not in g


def test_licence_normalized_spdx() -> None:
    g = _graph(dossier.render(_resource(license="cc-by-4.0")))
    lic = g["#licence"]
    assert lic["normalized_spdx"] == "CC-BY-4.0"
    assert lic["license_raw"] == "cc-by-4.0"


def test_licence_unrecognized_never_invented() -> None:
    g = _graph(dossier.render(_resource(license="see paper")))
    lic = g["#licence"]
    assert lic["normalized_spdx"] is None
    assert "unrecognized" in lic["value"]


def test_licence_omitted_when_absent() -> None:
    g = _graph(dossier.render(_resource(license=None)))
    assert "#licence" not in g


def test_fair_result_carries_scores_and_gaps() -> None:
    fa = FairAssessment(
        score=72,
        findable=80,
        accessible=60,
        interoperable=70,
        reusable=78,
        assessed=12,
        gaps=["metadata does not expose X (RDA-F1-01D)"],
    )
    g = _graph(dossier.render(_resource(fair=fa)))
    res = g["#fair"]
    assert res["score"] == 72
    assert res["findable"] == 80
    assert res["accessible"] == 60
    assert res["interoperable"] == 70
    assert res["reusable"] == 78
    assert res["gaps"] == ["metadata does not expose X (RDA-F1-01D)"]


def test_fair_omitted_when_absent() -> None:
    g = _graph(dossier.render(_resource(fair=None)))
    assert "#fair" not in g


# --- retraction HONESTY: unknown is never a negative claim -----------------


def test_retraction_unknown_makes_no_negative_claim() -> None:
    g = _graph(dossier.render(_resource(trust=TrustSignals())))
    res = g["#retraction"]
    assert res["retracted"] is None
    text = res["value"].lower()
    assert "unknown" in text
    # CRITICAL: must NOT assert a clean record from an unknown signal.
    assert "not retracted" not in text
    assert "no retraction on record" not in text


def test_retraction_true_names_the_doi() -> None:
    g = _graph(
        dossier.render(
            _resource(trust=TrustSignals(retracted=True, retraction_doi="10.1/retraction"))
        )
    )
    res = g["#retraction"]
    assert res["retracted"] is True
    assert res["retraction_doi"] == "10.1/retraction"
    assert "10.1/retraction" in res["value"]
    assert "retracted" in res["value"].lower()


def test_retraction_false_may_state_no_retraction() -> None:
    g = _graph(dossier.render(_resource(trust=TrustSignals(retracted=False, concern=False))))
    res = g["#retraction"]
    assert res["retracted"] is False
    assert "no retraction on record" in res["value"].lower()


def test_concern_unknown_makes_no_negative_claim() -> None:
    g = _graph(dossier.render(_resource(trust=TrustSignals(retracted=False))))
    res = g["#retraction"]
    # concern defaults to None → must read unknown, never "no expression of concern".
    assert res["concern"] is None
    assert "no expression of concern" not in res["value"].lower()


def test_retraction_omitted_when_trust_absent() -> None:
    g = _graph(dossier.render(_resource(trust=None)))
    assert "#retraction" not in g


# --- source / DOI / ID chain (always present) ------------------------------


def test_identifier_chain_carries_source_doi_links() -> None:
    g = _graph(
        dossier.render(
            _resource(
                identifiers={"pmid": "12345"},
                accessions=["GSE1"],
                links=[Link(rel="is_supplement_to", target_id="pmid:12345")],
            )
        )
    )
    res = g["#identifier-chain"]
    assert res["source"] == "zenodo"
    assert res["canonical_id"] == "zenodo:1"
    assert res["doi"] == "10.5281/zenodo.1"
    assert res["identifiers"] == {"pmid": "12345"}
    assert res["accessions"] == ["GSE1"]
    assert res["links"] == [{"rel": "is_supplement_to", "target_id": "pmid:12345"}]


def test_identifier_chain_referenced_from_action() -> None:
    crate = dossier.render(_resource())
    g = _graph(crate)
    refs = {ref["@id"] for ref in g["#provenance-assessment"]["result"]}
    assert "#identifier-chain" in refs


# --- live real-execution check ---------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_dossier_for_real_record() -> None:
    """Resolve a REAL Zenodo DOI end-to-end with format=provenance: the dossier must
    be well-formed RO-Crate 1.1, the FAIR/version/licence results must reflect real
    metadata, and an unknown retraction must make NO negative claim."""
    import httpx

    from data_aggregator_mcp import fair as fair_mod
    from data_aggregator_mcp import router
    from data_aggregator_mcp import trust as trust_mod

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        resource = await router.resolve(c, "10.5281/zenodo.3242074")
        resource = resource.model_copy(update={"fair": fair_mod.assess(resource)})
        resource = resource.model_copy(update={"trust": await trust_mod.annotate(c, resource)})
    crate = dossier.render(resource)
    g = _graph(crate)

    assert g["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    assert g["#provenance-assessment"]["@type"] == "CreateAction"
    assert "#fair" in g and 0 <= g["#fair"]["score"] <= 100
    retr = g["#retraction"]
    if retr["retracted"] is None:
        assert "not retracted" not in retr["value"].lower()
        assert "no retraction on record" not in retr["value"].lower()

    print("\n--- LIVE dossier (real record) ---")
    print("FAIR score:", g["#fair"]["score"])
    print("version:", g.get("#version-currency", {}).get("value", "<omitted>"))
    print("licence:", g.get("#licence", {}).get("value", "<omitted>"))
    print("retraction:", retr["value"])
