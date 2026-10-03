"""The Run Crate pinned whole (#88 mutation burn-down of `run_crate`).

`tests/test_run_crate.py` checks structure and reads a few fields. These tests compare
the crate's entities with literals written out by hand, so a garbled ``@type`` or
``name``, a dropped field or a wrong expansion axis fails. ``sources_queried`` names the
data sources seen to take part in the page, so a key of ``SearchResult.errors`` that is a
note about the run (``semantic``, ``filters``, ``query_syntax``, an ontology lookup) is
not a source. Expected values are never rebuilt with the code's own formulas.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from data_aggregator_mcp import __version__, fair, router, run_crate, sources
from data_aggregator_mcp.models import (
    AssayExpansion,
    ChemicalExpansion,
    FileEntry,
    MeshExpansion,
    SearchResult,
    TaxonExpansion,
    TissueExpansion,
)
from tests.test_run_crate import _graph, _resource, _search_result

# --- the whole crate --------------------------------------------------------

_EXPANSIONS: dict[str, Any] = {
    "taxon_expansion": TaxonExpansion(
        input="rice", taxid=4530, canonical_name="Oryza sativa", synonyms=["Asian rice"]
    ),
    "mesh_expansion": MeshExpansion(
        input="blast", mesh_ui="D000001", canonical_name="Blast Disease", synonyms=[]
    ),
    "tissue_expansion": TissueExpansion(
        input="leaf",
        uberon_id="PO:0025034",
        canonical_name="leaf",
        synonyms=["foliage", "blade"],
    ),
    "chemical_expansion": ChemicalExpansion(
        input="auxin", chebi_id="CHEBI:22676", canonical_name="auxin", synonyms=["IAA"]
    ),
    "assay_expansion": AssayExpansion(
        input="rna-seq",
        edam_id="EDAM:topic_3170",
        canonical_name="RNA-Seq",
        synonyms=["RNA sequencing"],
    ),
}


def _rich(**over: Any) -> SearchResult:
    """Two hits (one DOI'd and licensed, one with no DOI whose first file has no URL),
    every expansion axis fired, a failed stream of each shape and two run notes."""
    base = SearchResult(
        query="rice leaf",
        total=7,
        count=2,
        results=[
            _resource(id="zenodo:1", doi="10.5281/zenodo.1", title="Rice A"),
            _resource(
                id="geo:GSE1",
                source="geo",
                doi=None,
                title="Rice B",
                license=None,
                is_latest=None,
                files=[FileEntry(name="no-url"), FileEntry(name="m", url="https://u/m")],
            ),
        ],
        errors={
            "datacite": "ReadTimeout: slow",
            "omics/sra#v1": "HTTPStatusError: 503",
            "filters": "2 records removed by the kind filter",
            "semantic": "no embedding endpoint configured",
        },
        **_EXPANSIONS,
    )
    return base.model_copy(update=over)


def test_run_level_entities_pinned_whole() -> None:
    crate = run_crate.render(_rich())
    assert crate["@context"] == "https://w3id.org/ro/crate/1.1/context"
    graph = crate["@graph"]
    assert graph[0] == {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
        "about": {"@id": "./"},
    }
    assert graph[1] == {
        "@id": "./",
        "@type": "Dataset",
        "name": "Search run: rice leaf",
        "mentions": {"@id": "#search-action"},
        "hasPart": [{"@id": "#hit-0"}, {"@id": "#hit-1"}],
    }
    assert graph[2] == {
        "@id": "https://github.com/musharna/data-aggregator-mcp",
        "@type": "SoftwareApplication",
        "name": "data-aggregator-mcp",
        "version": __version__,
    }
    assert graph[3] == {
        "@id": "#search-action",
        "@type": "CreateAction",
        "name": "data-aggregator-mcp search",
        "instrument": {"@id": "https://github.com/musharna/data-aggregator-mcp"},
        "object": {"@id": "./"},
        "query": "rice leaf",
        "result_count": 2,
        "total": 7,
        # data sources only: the record sources, plus each failed stream named as its
        # records are (`omics/sra#v1` -> `sra`); the run notes are not sources.
        "sources_queried": ["datacite", "geo", "sra", "zenodo"],
        "result": [{"@id": "#hit-0"}, {"@id": "#hit-1"}],
        "ontology_expansions": [
            {
                "axis": "taxon",
                "input": "rice",
                "ontology_id": 4530,
                "canonical_name": "Oryza sativa",
                "synonyms": ["Asian rice"],
            },
            {
                "axis": "mesh",
                "input": "blast",
                "ontology_id": "D000001",
                "canonical_name": "Blast Disease",
                "synonyms": [],
            },
            {
                "axis": "tissue",
                "input": "leaf",
                "ontology_id": "PO:0025034",
                "canonical_name": "leaf",
                "synonyms": ["foliage", "blade"],
            },
            {
                "axis": "chemical",
                "input": "auxin",
                "ontology_id": "CHEBI:22676",
                "canonical_name": "auxin",
                "synonyms": ["IAA"],
            },
            {
                "axis": "assay",
                "input": "rna-seq",
                "ontology_id": "EDAM:topic_3170",
                "canonical_name": "RNA-Seq",
                "synonyms": ["RNA sequencing"],
            },
        ],
        "errors": {
            "datacite": "ReadTimeout: slow",
            "omics/sra#v1": "HTTPStatusError: 503",
            "filters": "2 records removed by the kind filter",
            "semantic": "no embedding endpoint configured",
        },
    }


def test_hit_entities_and_their_assessments_in_order() -> None:
    sr = _rich()
    crate = run_crate.render(sr)
    ids = [e["@id"] for e in crate["@graph"][4:]]
    assert ids == [
        "#hit-0",
        "#hit-0-version-currency",
        "#hit-0-licence",
        "#hit-0-fair",
        "#hit-0-identifier-chain",
        "#hit-1",
        "#hit-1-fair",
        "#hit-1-identifier-chain",
    ]
    g = _graph(crate)
    assert g["#hit-0"] == {
        "@id": "#hit-0",
        "@type": "Dataset",
        "name": "Rice A",
        "identifier": "https://doi.org/10.5281/zenodo.1",
        "mentions": [
            {"@id": "#hit-0-version-currency"},
            {"@id": "#hit-0-licence"},
            {"@id": "#hit-0-fair"},
            {"@id": "#hit-0-identifier-chain"},
        ],
        "license": "cc-by-4.0",
    }
    # No DOI and a first file without a URL: the first file URL there is.
    assert g["#hit-1"] == {
        "@id": "#hit-1",
        "@type": "Dataset",
        "name": "Rice B",
        "identifier": "https://u/m",
        "mentions": [{"@id": "#hit-1-fair"}, {"@id": "#hit-1-identifier-chain"}],
    }
    assert g["#hit-1-identifier-chain"] == {
        "@id": "#hit-1-identifier-chain",
        "@type": "PropertyValue",
        "name": "source-identifier-chain",
        "value": "source repository geo; canonical id geo:GSE1",
        "source": "geo",
        "canonical_id": "geo:GSE1",
    }
    # FAIR is assessed per hit, on that hit (not on the other one, not left unset).
    for i, hit in enumerate(sr.results):
        assert g[f"#hit-{i}-fair"]["score"] == fair.assess(hit).score
    assert g["#hit-0-fair"]["score"] != g["#hit-1-fair"]["score"]


def test_hit_identifier_is_the_canonical_id_when_no_file_has_a_url() -> None:
    sr = _rich(results=[_resource(id="geo:GSE2", doi=None, files=[FileEntry(name="a")])], count=1)
    assert _graph(run_crate.render(sr))["#hit-0"]["identifier"] == "geo:GSE2"


def test_only_the_axes_that_fired_are_echoed_in_order() -> None:
    sr = _rich(taxon_expansion=None, tissue_expansion=None, assay_expansion=None)
    exps = _graph(run_crate.render(sr))["#search-action"]["ontology_expansions"]
    assert [e["axis"] for e in exps] == ["mesh", "chemical"]


# --- sources_queried --------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("zenodo", "zenodo"),
        ("zenodo#v2", "zenodo"),
        ("omics/geo", "geo"),
        ("literature/pubmed#v0", "pubmed"),
        # notes about the run and failed ontology lookups are not data sources
        ("filters", None),
        ("semantic", None),
        ("query_syntax", None),
        ("understand", None),
        ("multi_query", None),
        ("taxonomy", None),
        ("mesh", None),
        ("uberon", None),
        ("chebi", None),
        ("edam", None),
        # keys that look like a stream but are not one the router makes
        ("zenodo#vx", None),
        ("zenodo#v", None),
        ("zenodo/sra", None),
        ("omics/nope", None),
        ("omics/", None),
        ("nope/geo", None),
    ],
)
def test_failed_source_reads_only_stream_keys(key: str, source: str | None) -> None:
    assert run_crate._failed_source(key) == source


@pytest.mark.parametrize("vi", [None, 3])
def test_failed_source_names_every_real_router_stream(vi: int | None) -> None:
    """Drift guard: the keys come from the router's own stream builder, so a change in
    how it labels a failed stream fails here instead of silently dropping the source."""
    streams = router._source_streams(
        None,  # type: ignore[arg-type]  # the calls are only built, never awaited
        sources.ADAPTERS,
        expanded="q",
        plain="q",
        filters={},
        pushdown=False,
        vi=vi,
    )
    named = {s.label: run_crate._failed_source(s.label) for s in streams}
    suffix = "" if vi is None else "#v3"
    assert named[f"zenodo{suffix}"] == "zenodo"
    assert named[f"omics/sra{suffix}"] == "sra"
    assert named[f"literature/openaire{suffix}"] == "openaire"
    # every stream the router can fail is named as a source
    assert None not in named.values()
    assert len(named) == len(streams)


def test_sources_queried_lists_sources_not_run_notes() -> None:
    sr = _search_result(
        errors={
            "semantic": "semantic re-rank unavailable",
            "filters": "filters (kind=dataset) could not be sent upstream",
            "taxonomy": "HTTPStatusError: 503",
            "omics/sra#v1": "ReadTimeout: slow",
            "datacite": "HTTPStatusError: 502",
        }
    )
    action = _graph(run_crate.render(sr))["#search-action"]
    # the two hit sources, plus the two failed streams named as their records name them
    assert action["sources_queried"] == ["datacite", "sra", "zenodo"]
    # every key is still disclosed, verbatim, under errors
    assert action["errors"] == sr.errors


def test_sources_queried_from_failures_alone() -> None:
    sr = _search_result(results=[], count=0, total=0, errors={"semantic": "x", "omics/geo": "y"})
    assert _graph(run_crate.render(sr))["#search-action"]["sources_queried"] == ["geo"]


# --- live real-execution check ---------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_run_note_is_not_a_source_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real semantic search with no embedding endpoint: the router reports
    ``errors["semantic"]`` beside live hits from zenodo and the omics sub-databases. The
    note is disclosed and is not named as a source; the hit sources are."""
    import httpx

    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        result = await router.search_page(
            c, query="rice genome", size=5, sources=["zenodo", "omics"], rank="semantic"
        )
    assert "semantic" in result.errors
    hit_sources = {h.source for h in result.results}
    assert hit_sources, "the live search returned no hits to name"

    action = _graph(run_crate.render(result))["#search-action"]
    assert action["errors"]["semantic"] == result.errors["semantic"]
    assert "semantic" not in action["sources_queried"]
    assert hit_sources <= set(action["sources_queried"])
    assert set(action["sources_queried"]) <= {"zenodo", "geo", "sra", "bioproject"}
