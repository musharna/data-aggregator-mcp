"""The Run Crate's run-level claims, pinned (#88 mutation burn-down of `run_crate`).

`tests/test_run_crate.py` checks structure and reads a few fields. These tests pin what
the crate says about the run itself, starting with ``sources_queried``: it names the data
sources seen to take part in the page, so a key of ``SearchResult.errors`` that is a note
about the run (``semantic``, ``filters``, ``query_syntax``, an ontology lookup) is not a
source. Expected values are written out by hand, never rebuilt with the code's formulas.
"""

from __future__ import annotations

import os

import pytest

from data_aggregator_mcp import router, run_crate, sources
from tests.test_run_crate import _graph, _search_result

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
