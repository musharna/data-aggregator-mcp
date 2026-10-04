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
_PAGE_ONE_ONLY = SimpleNamespace(PREFIXES=("stub", "stub2"), PAGINATES=False)


def test_every_declared_field_reaches_the_row_and_the_list_sources_entry() -> None:
    spec = sources._spec(
        "stub",
        _PAGE_ONE_ONLY,
        layer="omics",
        kinds=("study", "sequencing_run"),
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
        "filters_supported": ["query", "size"],  # page 1 only, keyword-only, no pushdown
        "auth_required": True,
        "rate_limit": "1/s",
        "status": "live (stub)",
        "fetchable": "per-stub",
        "operable": True,
        "fetchable_notes": "stub2 only.",
        "id_example": "stub:1 | stub2:2",
        "description": "A stub source.",
    }
    assert spec.module is _PAGE_ONE_ONLY
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
        rate_limit="none",
        status="live",
        id_example="stub:1",
    )
    assert spec.catalog_entry() == {
        "name": "stub",
        "layer": "archives",
        "kinds": ["dataset"],
        # an adapter that does not say otherwise pages and takes the expanded query
        "filters_supported": ["query", "size", "cursor", *sources.ONTOLOGY_FACETS],
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


def _pushdown_stub(pushes: set[str], kind_values: set[str]) -> SimpleNamespace:
    """An adapter whose pushable takes the filters in pushes, and kind only
    for the values in kind_values."""

    async def search(*_a: object, **_k: object) -> tuple[int, list[object]]:
        return 0, []

    def pushable(filters: dict[str, object], /) -> dict[str, object]:
        return {
            k: v for k, v in filters.items() if k in pushes and (k != "kind" or v in kind_values)
        }

    return SimpleNamespace(PREFIXES=("stub",), PAGINATES=False, search=search, pushable=pushable)


def test_filters_supported_lists_what_the_adapter_pushes_upstream() -> None:
    """Year bounds are listed one by one as pushable takes them; kind only when it
    is pushed for every kind the source carries (other is never asked for)."""

    def listed(module: SimpleNamespace, kinds: tuple[str, ...]) -> tuple[str, ...]:
        return sources._spec(
            "stub",
            module,
            layer="archives",
            kinds=kinds,
            rate_limit="none",
            status="live",
            id_example="stub:1",
            boolean_query=False,
        ).filters_supported

    some = _pushdown_stub({"published_before", "kind"}, {"study"})
    assert listed(some, ("study", "other")) == ("query", "size", "published_before", "kind")
    assert listed(some, ("study", "dataset")) == ("query", "size", "published_before")
    every = _pushdown_stub({"published_after", "published_before", "kind"}, {"study", "dataset"})
    assert listed(every, ("study", "dataset")) == (
        "query",
        "size",
        "published_after",
        "published_before",
        "kind",
    )
    # control: no pushdown at all lists neither years nor kind
    assert listed(_PAGE_ONE_ONLY, ("study",)) == ("query", "size")


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
