from __future__ import annotations

from data_aggregator_mcp import router, server, sources


def test_registry_is_the_adapter_map_in_order():
    assert list(sources.ADAPTERS) == [s.name for s in sources.SOURCES]
    assert router._ADAPTERS is sources.ADAPTERS  # router derives, not restates


def test_fetch_gate_matches_the_historical_hand_maintained_set():
    """The registry-derived fetch gate must reproduce exactly the prefixes that were
    hand-maintained in server._FETCHABLE_SOURCES before the registry existed."""
    assert set(sources.FETCHABLE_PREFIXES) == {
        "zenodo:",
        "sra:",
        "geo:",
        "datacite:",
        "pubmed:",
        "openaire:",
        "hf:",
        "dataone:",
        "gbif:",
        "datagov:",
        "omicsdi:",
        "dandi:",
        "cellxgene:",
        "openml:",
        "pdb:",
        "uniprot:",
        "biostudies:",
    }
    # omics routes bioproject for discovery but it is NOT fetchable — the prefix-granular case
    assert "bioproject:" not in sources.FETCHABLE_PREFIXES
    assert "bioproject" in sources.ADAPTERS["omics"].PREFIXES  # still routable
    assert server._FETCHABLE_SOURCES is sources.FETCHABLE_PREFIXES


def test_discovery_only_is_the_non_fetchable_sources():
    assert frozenset({"gwas", "nasacmr"}) == sources.DISCOVERY_ONLY
    assert router._DISCOVERY_ONLY_SOURCES is sources.DISCOVERY_ONLY


def test_resolver_for_routes_by_prefix():
    assert sources.resolver_for("uniprot") is sources.ADAPTERS["uniprot"]
    assert sources.resolver_for("geo") is sources.ADAPTERS["omics"]  # multi-prefix source
    assert sources.resolver_for("omicsdi") is sources.ADAPTERS["omicsdi"]
    assert sources.resolver_for("not-a-prefix") is None  # bare-id fallbacks handled by caller


def test_every_source_has_search_and_resolve():
    for spec in sources.SOURCES:
        assert callable(getattr(spec.module, "search", None)), spec.name
        assert callable(getattr(spec.module, "resolve", None)), spec.name
        assert spec.prefixes, spec.name  # every source declares at least one prefix


def test_server_SOURCES_names_do_not_drift_from_the_registry():
    """The rich human-facing _SOURCES metadata still lives in server, but its source set must
    stay in lockstep with the registry (a consistency guard until it, too, is folded in)."""
    assert {s["name"] for s in server._SOURCES} == {s.name for s in sources.SOURCES}
