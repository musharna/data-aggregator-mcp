from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

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


def test_server_SOURCES_is_the_registry_catalog():
    """The human-facing catalog is now DERIVED from the registry, not restated in server —
    so its source set cannot drift from the routing/fetch data."""
    assert server._SOURCES is sources.CATALOG
    assert {s["name"] for s in server._SOURCES} == {s.name for s in sources.SOURCES}


def test_catalog_order_covers_every_source_and_sets_the_payload_order():
    """CATALOG_ORDER is presentation order (deliberately != SOURCES' precedence order).
    Import fails loud if it ever misses a source; assert both halves here."""
    assert set(sources.CATALOG_ORDER) == {s.name for s in sources.SOURCES}
    assert [s["name"] for s in sources.CATALOG] == list(sources.CATALOG_ORDER)
    # The two orders really are different — otherwise this guard is vacuous.
    assert list(sources.CATALOG_ORDER) != [s.name for s in sources.SOURCES]


def test_catalog_entry_omits_unset_optional_keys_and_fixes_key_order():
    """list_sources is a public tool payload: optional keys stay ABSENT rather than None,
    and the key order is stable."""
    by_name = {s["name"]: s for s in sources.CATALOG}
    assert "description" not in by_name["zenodo"]  # never had one
    assert "fetchable_notes" not in by_name["zenodo"]
    assert "operable" not in by_name["omics"]  # sparse: omics/literature/omicsdi/gwas
    assert "description" not in by_name["datacite"]
    canonical = [
        "name",
        "layer",
        "kinds",
        "filters_supported",
        "auth_required",
        "rate_limit",
        "status",
        "fetchable",
        "operable",
        "fetchable_notes",
        "id_example",
        "description",
    ]
    for entry in sources.CATALOG:
        keys = list(entry)
        assert keys == [k for k in canonical if k in entry], entry["name"]


def test_advertised_fetchable_label_agrees_with_the_fetch_gate():
    """The advertised label and the gate are one declaration, so they cannot disagree —
    the failure mode where metadata promises a fetch the router refuses."""
    for spec in sources.SOURCES:
        assert bool(spec.fetchable) == bool(spec.fetchable_prefixes), spec.name
    assert {s.name for s in sources.SOURCES if s.fetchable is False} == sources.DISCOVERY_ONLY


def _stub_spec(**kw: Any) -> sources.SourceSpec:
    """Build a throwaway spec, varying only the fetchability declaration."""
    return sources._spec(
        "stub",
        SimpleNamespace(PREFIXES=frozenset({"stub"})),
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query",),
        rate_limit="none",
        status="live",
        id_example="stub:1",
        **kw,
    )


def test_spec_rejects_a_label_that_contradicts_the_gate():
    # advertises fetchable, but no prefix is actually gated
    with pytest.raises(ValueError, match="must agree"):
        _stub_spec(fetchable=True, fetchable_prefixes=())
    with pytest.raises(ValueError, match="must agree"):
        _stub_spec(fetchable="per-repo", fetchable_prefixes=())
    # declared discovery-only, yet names fetchable prefixes
    with pytest.raises(ValueError, match="fetchable=False"):
        _stub_spec(fetchable=False, fetchable_prefixes=("stub",))
    # the consistent cases still build
    assert _stub_spec(fetchable=False).fetchable_prefixes == frozenset()
    assert _stub_spec().fetchable_prefixes == frozenset({"stub"})


# ------------------------------------------------------------------ adapter contract


def test_every_registered_adapter_satisfies_the_protocol():
    """`module` was typed `Any`, so a registered module missing `resolve` type-checked
    fine and only failed at runtime on the id that happened to route to it."""
    for spec in sources.SOURCES:
        assert isinstance(spec.module, sources.SourceAdapter), spec.name


def test_protocol_membership_is_not_vacuous():
    """Guard the guard: if the Protocol had no required members, the check above would
    pass for literally any object."""
    missing_resolve = SimpleNamespace(PREFIXES=frozenset({"x"}), search=lambda *a, **k: None)
    assert not isinstance(missing_resolve, sources.SourceAdapter)
    assert not isinstance(SimpleNamespace(), sources.SourceAdapter)


def test_every_adapter_has_the_call_shape_the_router_uses():
    """`runtime_checkable` only checks attribute PRESENCE — a drifted signature still
    satisfies isinstance. The router calls `search(client, query, size=, offset=)` and
    `resolve(client, id)`, so pin that shape for every source."""
    import inspect

    for spec in sources.SOURCES:
        for hook in ("search", "resolve"):
            fn = getattr(spec.module, hook)
            assert inspect.iscoroutinefunction(fn), f"{spec.name}.{hook} is not async"

        params = list(inspect.signature(spec.module.search).parameters.values())
        assert [p.name for p in params[:2]][0] == "client", spec.name
        assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in params[:2]), spec.name
        by_name = {p.name: p for p in params}
        for kw in ("size", "offset"):
            assert by_name[kw].kind is inspect.Parameter.KEYWORD_ONLY, f"{spec.name}.{kw}"
            assert by_name[kw].default is not inspect.Parameter.empty, f"{spec.name}.{kw}"

        resolve_params = list(inspect.signature(spec.module.resolve).parameters.values())
        assert len(resolve_params) == 2, spec.name
        assert resolve_params[0].name == "client", spec.name
        # The second name deliberately varies (record_id / resource_id), which is why the
        # Protocol declares both parameters positional-only.
        assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in resolve_params), (
            spec.name
        )


def test_no_advertised_id_example_is_a_placeholder():
    """`list_sources` is the model's discovery surface: an example it cannot resolve sends
    it straight into an error. Three shipped as placeholders (`datacite:10.5061/dryad.x`,
    `cellxgene:col-lung-1`, a literal `openaire:<id>`) — the first indistinguishable from a
    real DOI. Offline shape guard; the live counterpart actually resolves them."""
    routable = {p for spec in sources.SOURCES for p in spec.prefixes}
    for spec in sources.SOURCES:
        for part in (p.strip() for p in spec.id_example.split("|")):
            assert "<" not in part and ">" not in part, f"{spec.name}: placeholder in {part!r}"
            prefix = part.split(":", 1)[0]
            assert prefix in routable, f"{spec.name}: {part!r} has unroutable prefix {prefix!r}"
            assert part.split(":", 1)[1].strip(), f"{spec.name}: {part!r} has an empty local id"


# --- boolean_query: a declaration about someone else's parser, so probe it live ------

# One query per registered source that has hits on its own upstream. The live probe
# below sends it plain and inside a semantically NEUTRAL ontology-shaped expansion
# ``(q) AND ("q" OR "q")``: a boolean parser returns hits for both, a keyword-only one
# substring-matches the literal (0 hits) or rejects it (HTTP 4xx/5xx).
BOOLEAN_PROBE_QUERY = {
    "zenodo": "arabidopsis",
    "dataone": "soil",
    "gbif": "amphibian",
    "datagov": "climate",
    "cellxgene": "lung",
    "datacite": "arabidopsis",
    "dandi": "mouse",
    "omics": "arabidopsis",
    "literature": "arabidopsis",
    "huggingface": "arabidopsis",
    "omicsdi": "proteome",
    "openml": "iris",
    "pdb": "hemoglobin",
    "uniprot": "insulin",
    "gwas": "Type 2 diabetes",
    "nasacmr": "sea surface temperature",
    "biostudies": "arabidopsis",
}


def test_every_source_has_a_boolean_probe_query():
    """B-H6 (audit 2026-09-27): ``boolean_query`` defaults to True, so a new source is
    ASSUMED to parse the ontology expansion. M9 (2026-09-22) fixed four sources by hand
    and missed gwas + dandi, which zeroed silently (148 → 0, 66 → 0) under any
    organism/disease/tissue param. A source added without a probe query is a declaration
    nothing checks — fail here so the live probe covers it."""
    assert set(BOOLEAN_PROBE_QUERY) == {s.name for s in sources.SOURCES}


def test_keyword_only_set_is_the_live_probed_set():
    """Pinned from the live probe (2026-09-27; plain → expanded hits): cellxgene 58→0,
    dandi 325→0, huggingface 5→0, openml 5→HTTP 500, gwas 148→0, nasacmr 4115→HTTP 400;
    every other source kept its hits (pdb 9193→4112, omics 504008→435752 — phrase
    matching, still parsed)."""
    assert {
        "cellxgene",
        "huggingface",
        "openml",
        "nasacmr",
        "gwas",
        "dandi",
    } == sources.KEYWORD_ONLY
    # positive control: the boolean-capable majority still gets the expansion
    assert {"zenodo", "datacite", "omics", "pdb", "uniprot"}.isdisjoint(sources.KEYWORD_ONLY)


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


@pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
@pytest.mark.parametrize("spec", sources.SOURCES, ids=lambda s: s.name)
async def test_live_boolean_query_declaration_matches_the_upstream(spec) -> None:
    import httpx

    from data_aggregator_mcp import _ontology
    from data_aggregator_mcp.errors import RateLimitError, UpstreamUnavailableError

    q = BOOLEAN_PROBE_QUERY[spec.name]
    expanded = f"({q}) AND ({_ontology.or_group([q, q])})"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        try:
            total, recs = await spec.module.search(client, q, size=3)
        except (RateLimitError, UpstreamUnavailableError) as e:
            pytest.skip(f"{spec.name}: plain-query control unavailable, probe inconclusive: {e}")
        assert total > 0 and recs, f"{spec.name}: probe query {q!r} has no hits — pick another"
        try:
            etotal, erecs = await spec.module.search(client, expanded, size=3)
            parses = etotal > 0 and bool(erecs)
        except UpstreamUnavailableError:  # 4xx/5xx on the boolean string = cannot parse
            parses = False
    assert parses == spec.boolean_query, (
        f"{spec.name}: boolean_query={spec.boolean_query} but the expansion "
        f"{'kept' if parses else 'lost'} its hits ({total} plain)"
    )
