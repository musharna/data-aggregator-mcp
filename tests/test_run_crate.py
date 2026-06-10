"""Tests for the whole-search RO-Crate 1.1 Run Crate (B10b).

Covers: the dossier refactor (assessment_entities reuse seam + B10a byte-identical),
the pure run_crate.render structure/honesty, and the search(provenance=true) wiring.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

from data_aggregator_mcp import __version__, dossier, run_crate, server
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    MeshExpansion,
    SearchResult,
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
        is_latest=True,
        files=[FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=10)],
    )
    return base.model_copy(update=over)


def _graph(crate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    graph: list[dict[str, Any]] = crate["@graph"]
    return {e["@id"]: e for e in graph}


def _search_result(**over: Any) -> SearchResult:
    base = SearchResult(
        query="rice genome",
        total=2,
        count=2,
        results=[
            _resource(id="zenodo:1", doi="10.5281/zenodo.1", title="Rice genome A"),
            _resource(
                id="datacite:2",
                source="datacite",
                doi="10.5061/dryad.x",
                title="Rice genome B",
                license=None,
                is_latest=None,
            ),
        ],
        errors={},
    )
    return base.model_copy(update=over)


# --- dossier refactor regression -------------------------------------------


def test_assessment_entities_matches_render_embedding() -> None:
    """assessment_entities(r, "") returns the SAME entities render embeds in its action."""
    r = _resource()
    entities = dossier.assessment_entities(r, "")
    crate = dossier.render(r)
    g = _graph(crate)
    action = g["#provenance-assessment"]
    ref_ids = [ref["@id"] for ref in action["result"]]
    assert ref_ids == [e["@id"] for e in entities]
    # And the embedded entities are byte-equal to what the seam produces.
    for e in entities:
        assert g[e["@id"]] == e


def test_dossier_render_ids_unchanged_default_prefix() -> None:
    """B10a @ids stay literal — the id_prefix="" default keeps render byte-identical."""
    g = _graph(dossier.render(_resource(trust=TrustSignals())))
    for eid in (
        "#version-currency",
        "#licence",
        "#identifier-chain",
        "#retraction",
    ):
        assert eid in g


def test_assessment_entities_prefix_applied() -> None:
    entities = dossier.assessment_entities(_resource(trust=TrustSignals()), "hit-3-")
    ids = {e["@id"] for e in entities}
    assert "#hit-3-version-currency" in ids
    assert "#hit-3-licence" in ids
    assert "#hit-3-retraction" in ids
    assert "#hit-3-identifier-chain" in ids
    # No un-prefixed leakage.
    assert "#version-currency" not in ids


# --- purity / determinism --------------------------------------------------


def test_render_signature_takes_only_result() -> None:
    params = list(inspect.signature(run_crate.render).parameters)
    assert params == ["result"]


def test_render_is_deterministic() -> None:
    sr = _search_result()
    assert run_crate.render(sr) == run_crate.render(sr)


def test_render_does_not_import_httpx() -> None:
    """run_crate's module path must not pull in network machinery."""
    import sys

    # run_crate itself imports only pure modules; assert it has no httpx attribute.
    assert not hasattr(run_crate, "httpx")
    assert "data_aggregator_mcp.run_crate" in sys.modules


# --- structural RO-Crate 1.1 validity --------------------------------------


def test_structure_context_and_graph() -> None:
    crate = run_crate.render(_search_result())
    assert crate["@context"] == "https://w3id.org/ro/crate/1.1/context"
    assert isinstance(crate["@graph"], list)


def test_conforms_to_only_on_descriptor() -> None:
    crate = run_crate.render(_search_result())
    carriers = [e["@id"] for e in crate["@graph"] if "conformsTo" in e]
    assert carriers == ["ro-crate-metadata.json"]
    g = _graph(crate)
    assert g["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    assert g["ro-crate-metadata.json"]["about"] == {"@id": "./"}


def test_root_names_query_and_haspart_hits() -> None:
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    root = g["./"]
    assert root["@type"] == "Dataset"
    assert "rice genome" in root["name"]
    assert root["mentions"] == {"@id": "#search-action"}
    assert root["hasPart"] == [{"@id": "#hit-0"}, {"@id": "#hit-1"}]


def test_search_action_has_instrument_object() -> None:
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    action = g["#search-action"]
    assert action["@type"] == "CreateAction"
    assert action["instrument"] == {"@id": dossier.AGENT_ID}
    assert action["object"] == {"@id": "./"}
    assert action["result"] == [{"@id": "#hit-0"}, {"@id": "#hit-1"}]


def test_agent_carries_version() -> None:
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    agent = g[dossier.AGENT_ID]
    assert agent["@type"] == "SoftwareApplication"
    assert agent["version"] == __version__


# --- run provenance: query / errors / expansions ---------------------------


def test_action_encodes_query() -> None:
    g = _graph(run_crate.render(_search_result(query="oryza sativa")))
    assert g["#search-action"]["query"] == "oryza sativa"


def test_action_discloses_errors() -> None:
    g = _graph(run_crate.render(_search_result(errors={"zenodo": "503 upstream"})))
    action = g["#search-action"]
    assert action["errors"] == {"zenodo": "503 upstream"}
    # The errored source is reflected in sources_queried.
    assert "zenodo" in action["sources_queried"]


def test_action_no_errors_block_when_clean() -> None:
    g = _graph(run_crate.render(_search_result(errors={})))
    assert "errors" not in g["#search-action"]


def test_action_shows_expansion_that_fired() -> None:
    mesh = MeshExpansion(
        input="breast cancer",
        mesh_ui="D001943",
        canonical_name="Breast Neoplasms",
        synonyms=["Breast Tumor"],
    )
    g = _graph(run_crate.render(_search_result(mesh_expansion=mesh)))
    exps = g["#search-action"]["ontology_expansions"]
    assert len(exps) == 1
    assert exps[0]["axis"] == "mesh"
    assert exps[0]["ontology_id"] == "D001943"
    assert exps[0]["input"] == "breast cancer"
    assert exps[0]["synonyms"] == ["Breast Tumor"]


def test_action_no_expansion_block_when_none_fired() -> None:
    g = _graph(run_crate.render(_search_result()))
    assert "ontology_expansions" not in g["#search-action"]


# --- per-hit provenance -----------------------------------------------------


def test_n_hits_yield_n_hit_entities() -> None:
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    assert g["#hit-0"]["@type"] == "Dataset"
    assert g["#hit-1"]["@type"] == "Dataset"
    assert g["#hit-0"]["name"] == "Rice genome A"
    assert g["#hit-0"]["identifier"] == "https://doi.org/10.5281/zenodo.1"


def test_hit_carries_version_licence_fair_assessments() -> None:
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    # hit-0 has license + is_latest=True → version, licence, fair, identifier present.
    assert g["#hit-0-version-currency"]["is_latest"] is True
    assert g["#hit-0-licence"]["normalized_spdx"] == "CC-BY-4.0"
    assert 0 <= g["#hit-0-fair"]["score"] <= 100
    assert g["#hit-0-identifier-chain"]["source"] == "zenodo"
    # Linked from the hit entity for navigability.
    mentioned = {ref["@id"] for ref in g["#hit-0"]["mentions"]}
    assert "#hit-0-fair" in mentioned


def test_per_hit_retraction_absent() -> None:
    """Search hits carry trust=None → NO retraction entity, NO negative claim."""
    crate = run_crate.render(_search_result())
    g = _graph(crate)
    assert "#hit-0-retraction" not in g
    assert "#hit-1-retraction" not in g
    # No negative retraction string anywhere in the crate.
    import json

    blob = json.dumps(crate).lower()
    assert "no retraction on record" not in blob
    assert "not retracted" not in blob


def test_empty_result_set_valid_zero_hit_crate() -> None:
    crate = run_crate.render(_search_result(results=[], total=0, count=0))
    g = _graph(crate)
    assert g["./"]["hasPart"] == []
    assert g["#search-action"]["result"] == []
    assert "#hit-0" not in g


# --- honesty inherited from the dossier helpers ----------------------------


def test_hit_no_version_entity_when_is_latest_none() -> None:
    g = _graph(run_crate.render(_search_result()))
    # hit-1 has is_latest=None → no version entity.
    assert "#hit-1-version-currency" not in g


def test_hit_no_licence_entity_when_licence_absent() -> None:
    g = _graph(run_crate.render(_search_result()))
    # hit-1 has license=None → no licence entity.
    assert "#hit-1-licence" not in g


def test_hit_unrecognized_licence_never_invented() -> None:
    sr = _search_result(
        results=[_resource(id="zenodo:1", license="see the paper", is_latest=None)],
        total=1,
        count=1,
    )
    g = _graph(run_crate.render(sr))
    lic = g["#hit-0-licence"]
    assert lic["normalized_spdx"] is None
    assert "unrecognized" in lic["value"]


def test_hit_identifier_falls_back_to_url_then_id() -> None:
    sr = _search_result(
        results=[
            _resource(id="zenodo:9", doi=None, files=[FileEntry(name="x", url="https://u/x")]),
            _resource(id="zenodo:10", doi=None, files=[]),
        ],
        total=2,
        count=2,
    )
    g = _graph(run_crate.render(sr))
    assert g["#hit-0"]["identifier"] == "https://u/x"
    assert g["#hit-1"]["identifier"] == "zenodo:10"


# --- search wiring ----------------------------------------------------------


def test_search_tool_exposes_provenance_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    prop = tool.inputSchema["properties"]["provenance"]
    assert prop["type"] == "boolean"
    # Not required (opt-in).
    assert "provenance" not in tool.inputSchema.get("required", [])


async def test_dispatch_search_attaches_crate_when_true(monkeypatch) -> None:
    async def fake_search_page(client, **kwargs):
        return _search_result(query=kwargs.get("query"))

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    out = await server._dispatch("search", {"query": "rice", "provenance": True})
    assert out["provenance_crate"] is not None
    assert out["provenance_crate"]["@context"] == "https://w3id.org/ro/crate/1.1/context"
    g = {e["@id"]: e for e in out["provenance_crate"]["@graph"]}
    assert g["#search-action"]["query"] == "rice"


async def test_dispatch_search_no_crate_by_default(monkeypatch) -> None:
    async def fake_search_page(client, **kwargs):
        return _search_result(query=kwargs.get("query"))

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    out = await server._dispatch("search", {"query": "rice"})
    assert out["provenance_crate"] is None
    out_false = await server._dispatch("search", {"query": "rice", "provenance": False})
    assert out_false["provenance_crate"] is None


# --- live real-execution check ---------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_run_crate_for_real_search() -> None:
    """Run a REAL search_page then render: a well-formed RO-Crate 1.1 with >=1 hit,
    per-hit FAIR present, the query encoded, and NO per-hit negative retraction claim."""
    import json

    import httpx

    from data_aggregator_mcp import router

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        result = await router.search_page(c, query="rice genome", size=5)
    crate = run_crate.render(result)
    g = _graph(crate)

    assert g["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    assert g["#search-action"]["query"] == "rice genome"
    assert len(result.results) >= 1
    # At least the first hit carries a FAIR assessment.
    assert "#hit-0-fair" in g and 0 <= g["#hit-0-fair"]["score"] <= 100
    # No per-hit negative retraction claim anywhere.
    blob = json.dumps(crate).lower()
    assert "no retraction on record" not in blob
    assert "not retracted" not in blob

    print("\n--- LIVE run crate (real search) ---")
    print("hits:", len(result.results))
    print("hit-0 FAIR:", g["#hit-0-fair"]["score"])
    print("sources_queried:", g["#search-action"]["sources_queried"])
