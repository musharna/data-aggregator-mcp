"""What a registry row declares is what the source is: ``_spec`` builds every row, so each
argument it is given must reach the ``list_sources`` row and the fields the router, fetch
gate and licence check read.

The real rows are built at import, before mutmut switches a mutant on, so no test over
``sources.SOURCES`` can see ``_spec`` drop or default an argument. These tests call it
directly, with values no real row uses, and compare with literals written here.
"""

from __future__ import annotations

from types import SimpleNamespace

from data_aggregator_mcp import sources

_MODULE = SimpleNamespace(PREFIXES=("stub", "stub2"))


def test_every_declared_field_reaches_the_row_and_the_list_sources_entry() -> None:
    spec = sources._spec(
        "stub",
        _MODULE,
        layer="omics",
        kinds=("study", "sequencing_run"),
        filters_supported=("query", "organism"),
        rate_limit="1/s",
        status="live (stub)",
        id_example="stub:1 | stub2:2",
        fetchable="per-stub",
        fetchable_prefixes=("stub2",),
        auth_required=True,
        operable=True,
        fetchable_notes="stub2 only.",
        description="A stub source.",
        default_license="CC0-1.0",
        default_license_policy="Stub policy — https://example.org/policy",
        boolean_query=False,
    )
    assert spec.catalog_entry() == {
        "name": "stub",
        "layer": "omics",
        "kinds": ["study", "sequencing_run"],
        "filters_supported": ["query", "organism"],
        "auth_required": True,
        "rate_limit": "1/s",
        "status": "live (stub)",
        "fetchable": "per-stub",
        "operable": True,
        "fetchable_notes": "stub2 only.",
        "id_example": "stub:1 | stub2:2",
        "description": "A stub source.",
    }
    assert spec.module is _MODULE
    assert spec.prefixes == frozenset({"stub", "stub2"})
    assert spec.fetchable_prefixes == frozenset({"stub2"})
    assert spec.default_license == "CC0-1.0"
    assert spec.default_license_policy == "Stub policy — https://example.org/policy"
    assert spec.boolean_query is False


def test_an_undeclared_field_takes_the_default_every_real_row_relies_on() -> None:
    """No real row sets ``auth_required`` and eleven leave ``boolean_query`` unset, so these
    defaults ARE what ``list_sources`` and the router's ontology expansion say about them."""
    spec = sources._spec(
        "stub",
        _MODULE,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query",),
        rate_limit="none",
        status="live",
        id_example="stub:1",
    )
    assert spec.catalog_entry() == {
        "name": "stub",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": ["query"],
        "auth_required": False,
        "rate_limit": "none",
        "status": "live",
        "fetchable": True,
        "id_example": "stub:1",
    }
    assert spec.fetchable_prefixes == frozenset({"stub", "stub2"})
    assert spec.boolean_query is True
    assert spec.default_license is None
    assert spec.default_license_policy is None


def test_every_prefix_routes_to_the_one_source_that_declares_it() -> None:
    """``resolver_for`` takes the first row that lists a prefix, so a prefix two sources
    claimed would route every id to the first and leave the second unreachable, and a
    fetchable prefix no row routes would pass the fetch gate and fail at resolve."""
    for spec in sources.SOURCES:
        for prefix in spec.prefixes:
            assert sources.resolver_for(prefix) is spec.module, (spec.name, prefix)
        assert spec.fetchable_prefixes <= spec.prefixes, spec.name
    for gated in sources.FETCHABLE_PREFIXES:
        assert sources.resolver_for(gated.removesuffix(":")) is not None, gated
    assert sources.resolver_for("stub") is None  # control: an undeclared prefix is unrouted
