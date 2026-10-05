"""Router behaviour the nightly mutation run showed no test observed (#88).

Each test pins something a caller relies on and a surviving mutant could change
without any test failing: the settings a cursor carries past page 2, the defaults an
old cursor continues with, the client every upstream call is made with, the
understand echo, the paging accounting, and the exact text of every rejection and
advisory note.
"""

from __future__ import annotations

import logging
from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest

from data_aggregator_mcp import (
    _cursor,
    anatomy,
    assay,
    chemistry,
    embeddings,
    mesh,
    router,
    taxonomy,
)
from data_aggregator_mcp.errors import UpstreamUnavailableError, ValidationError
from data_aggregator_mcp.models import DataResource, Link, QueryUnderstanding, Taxon
from data_aggregator_mcp.query_understanding import ParsedRewrite

_FACETS = {
    "organism": "Zea mays",
    "disease": "breast cancer",
    "tissue": "liver",
    "chemical": "caffeine",
    "assay": "ChIP-seq",
}
# The OR-group each facet ANDs onto the query when its lookup resolves.
_GROUPS = (
    '("Zea mays" OR "maize")',
    '("Breast Neoplasms" OR "Breast Cancer")',
    '("liver" OR "iecur")',
    '("caffeine" OR "theine")',
    '("ChIP-seq" OR "ChIP-exo")',
)
_PLANT = taxonomy.TaxonInfo(
    taxid=4577, canonical_name="Zea mays", synonyms=("maize",), is_plant=True
)


def _client() -> httpx.AsyncClient:
    """A client that can reach nothing: an upstream a test forgot to fake fails loudly."""
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(599)))


def _resolve_every_facet(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_PLANT))
    monkeypatch.setattr(
        mesh,
        "resolve_mesh",
        AsyncMock(
            return_value=mesh.MeshInfo(
                ui="D001943", canonical="Breast Neoplasms", synonyms=("Breast Cancer",)
            )
        ),
    )
    monkeypatch.setattr(
        anatomy,
        "resolve_uberon",
        AsyncMock(
            return_value=anatomy.UberonInfo(
                uberon_id="UBERON:0002107", canonical="liver", synonyms=("iecur",)
            )
        ),
    )
    monkeypatch.setattr(
        chemistry,
        "resolve_chebi",
        AsyncMock(
            return_value=chemistry.ChebiInfo(
                chebi_id="CHEBI:27732", canonical="caffeine", synonyms=("theine",)
            )
        ),
    )
    monkeypatch.setattr(
        assay,
        "resolve_edam",
        AsyncMock(
            return_value=assay.EdamInfo(
                edam_id="EDAM:topic_3169", canonical="ChIP-seq", synonyms=("ChIP-exo",)
            )
        ),
    )


def _rec(id_: str, **kw) -> DataResource:
    kw.setdefault("source", id_.split(":", 1)[0])
    kw.setdefault("title", id_)
    return DataResource(id=id_, kind="dataset", year=2020, **kw)


class _Upstream:
    """A fake adapter ``search`` holding ``total`` records per query, ``<name>:<query>:<i>``
    unless ``make`` builds them. Records every call, so a test sees what page 2 was sent."""

    def __init__(self, name: str, total: int = 100, make=None) -> None:
        self.name, self.total, self.calls = name, total, []
        self.make = make or (lambda query, i: _rec(f"{name}:{query}:{i}"))

    async def search(self, client, query, *, size, offset=0, **kw):
        self.calls.append({"client": client, "query": query, "offset": offset, **kw})
        stop = min(offset + size, self.total)
        return self.total, [self.make(query, i) for i in range(offset, stop)]


def _serve(monkeypatch, name: str, **kw) -> _Upstream:
    up = _Upstream(name, **kw)
    monkeypatch.setattr(router._ADAPTERS[name], "search", up.search)
    return up


def _settings(state: dict) -> dict:
    """A cursor's settings: everything but the position it records."""
    return {k: v for k, v in state.items() if k not in ("offsets", "ahead")}


# --- the cursor carries every setting, not only to page 2 ----------------------------


async def test_every_search_setting_survives_into_the_page_after_next(monkeypatch) -> None:
    """Page 2 reads its settings back from the cursor and writes them into page 3's. A
    setting dropped on that read changed nothing on page 2 that a test looked at, and
    was gone from page 3 on."""
    _resolve_every_facet(monkeypatch)
    up = _serve(monkeypatch, "zenodo")
    anchors: list[str] = []

    async def rerank(client, query, resources):
        anchors.append(query)
        return resources, None

    monkeypatch.setattr(embeddings, "rerank", rerank)
    async with _client() as client:
        p1 = await router.search_page(
            client,
            query="rna",
            size=2,
            sources=["zenodo"],
            published_after=2000,
            kind="dataset",
            rank="semantic",
            collapse_mirrors=True,
            **_FACETS,
        )
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    expanded = up.calls[0]["query"]
    assert all(group in expanded for group in _GROUPS)
    c1, c2 = _cursor.decode(p1.next_cursor), _cursor.decode(p2.next_cursor)
    assert _settings(c1) == {
        "q": "rna",
        "eq": expanded,
        "sources": ["zenodo"],
        **_FACETS,
        "fg": [
            ["Zea mays", ["Zea mays", "maize"]],
            ["breast cancer", ["Breast Neoplasms", "Breast Cancer"]],
            ["liver", ["liver", "iecur"]],
            ["caffeine", ["caffeine", "theine"]],
            ["ChIP-seq", ["ChIP-seq", "ChIP-exo"]],
        ],
        "filters": {"published_after": 2000, "published_before": None, "kind": "dataset"},
        "size": 2,
        "rank": "semantic",
        "collapse_mirrors": True,
        "pd": True,
    }
    assert _settings(c2) == _settings(c1)
    # Page 2 searched the same expanded query with the same pushed filters, one page on,
    # and re-ranked against the raw query as page 1 did.
    assert [(c["query"], c["offset"], c["filters"]) for c in up.calls] == [
        (expanded, 0, {"published_after": 2000, "kind": "dataset"}),
        (expanded, 2, {"published_after": 2000, "kind": "dataset"}),
    ]
    assert anchors == ["rna", "rna"]
    assert p1.unresolved == []  # all five resolved: nothing to report


async def test_every_facet_that_matched_nothing_is_reported_in_order(monkeypatch) -> None:
    for module, name in (
        (taxonomy, "resolve_taxon"),
        (mesh, "resolve_mesh"),
        (anatomy, "resolve_uberon"),
        (chemistry, "resolve_chebi"),
        (assay, "resolve_edam"),
    ):
        monkeypatch.setattr(module, name, AsyncMock(return_value=None))
    _serve(monkeypatch, "zenodo", total=0)
    async with _client() as client:
        page = await router.search_page(client, query="rna", sources=["zenodo"], **_FACETS)
    assert [(u.field, u.input) for u in page.unresolved] == list(_FACETS.items())


async def test_the_query_syntax_note_names_every_keyword_only_source_only_when_expanded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_PLANT))
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    for name in ("zenodo", "dandi", "openml"):
        _serve(monkeypatch, name, total=0)
    kw = {"query": "rna", "sources": ["zenodo", "dandi", "openml"], "multi_query": True}
    async with _client() as client:
        plain = await router.search_page(client, **kw)
        expanded = await router.search_page(client, organism="Zea mays", **kw)
    assert "query_syntax" not in plain.errors  # nothing was expanded, so nothing was withheld
    assert expanded.errors["query_syntax"].startswith(
        "dandi, openml cannot parse boolean queries, so the ontology expansion "
    )


async def test_a_multi_query_cursor_without_the_later_keys_continues_unfolded_without_pushdown(
    monkeypatch,
) -> None:
    """A multi-query cursor minted before collapse_mirrors and pd were stored."""
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    up = _serve(monkeypatch, "zenodo")
    old = _cursor.encode(
        {
            "q": "rna",
            "variants": ["rna", "alt"],
            "sources": ["zenodo"],
            "filters": {"published_after": 2000},
            "size": 2,
            "offsets": {"0:zenodo": 2, "1:zenodo": 2},
        }
    )
    async with _client() as client:
        page = await router.search_page(client, cursor=old)
    assert sorted((c["query"], c["offset"], "filters" in c) for c in up.calls) == [
        ("alt", 2, False),
        ("rna", 2, False),
    ]
    state = _cursor.decode(page.next_cursor)
    assert (state["collapse_mirrors"], state["pd"]) == (False, False)


async def test_a_cursor_holding_only_the_required_fields_continues_with_the_defaults(
    monkeypatch,
) -> None:
    """A cursor minted before rank, collapse_mirrors, eq and pd existed holds none of them:
    it continues by relevance, unfolded, on its raw query, without filter pushdown."""
    up = _serve(monkeypatch, "zenodo")
    monkeypatch.setattr(embeddings, "rerank", AsyncMock(side_effect=AssertionError("re-ranked")))
    old = _cursor.encode(
        {
            "q": "rna",
            "size": 2,
            "offsets": {"zenodo": 2},
            "sources": ["zenodo"],
            "filters": {"published_after": 2000},
        }
    )
    async with _client() as client:
        page = await router.search_page(client, cursor=old)
    assert [(c["query"], c["offset"], "filters" in c) for c in up.calls] == [("rna", 2, False)]
    state = _cursor.decode(page.next_cursor)
    assert (state["eq"], state["rank"], state["collapse_mirrors"], state["pd"]) == (
        "rna",
        "relevance",
        False,
        False,
    )


async def test_a_fresh_search_ranks_by_relevance_and_keeps_mirrors_by_default(
    monkeypatch,
) -> None:
    _serve(monkeypatch, "zenodo")
    async with _client() as client:
        page = await router.search_page(client, query="rna", size=2, sources=["zenodo"])
    state = _cursor.decode(page.next_cursor)
    assert (state["rank"], state["collapse_mirrors"]) == ("relevance", False)


async def test_a_multi_query_cursor_carries_every_setting_to_the_page_after_next(
    monkeypatch,
) -> None:
    """The multi-query cursor's settings, read back on page 2 and written into page 3's:
    sources, both variant lists, filters, re-rank skips ("ahead"), folding, pushdown."""
    _resolve_every_facet(monkeypatch)
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    zen = _serve(monkeypatch, "zenodo")
    dandi = _serve(monkeypatch, "dandi")  # keyword-only: sent the variant before expansion

    async def rerank(client, query, resources):
        return list(reversed(resources)), None  # emits a window's tail first → "ahead"

    monkeypatch.setattr(embeddings, "rerank", rerank)
    async with _client() as client:
        p1 = await router.search_page(
            client,
            query="rna",
            organism="Zea mays",
            size=2,
            sources=["zenodo", "dandi"],
            published_after=2000,
            collapse_mirrors=True,
            multi_query=True,
        )
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    c1, c2 = _cursor.decode(p1.next_cursor), _cursor.decode(p2.next_cursor)
    group = '("Zea mays" OR "maize")'
    assert _settings(c1) == {
        "q": "rna",
        "sources": ["zenodo", "dandi"],
        "variants": [f"(rna) AND {group}", f"(alt) AND {group}"],
        "raw_variants": ["rna", "alt"],
        "fg": [["Zea mays", ["Zea mays", "maize"]]],
        "filters": {"published_after": 2000, "published_before": None, "kind": None},
        "size": 2,
        "collapse_mirrors": True,
        "pd": True,
    }
    assert c1["ahead"]  # the reversed window left positions returned out of order
    assert _settings(c2) == _settings(c1)
    assert {r.id for r in p1.results}.isdisjoint(r.id for r in p2.results)
    assert sorted(c["query"] for c in dandi.calls) == ["alt", "alt", "rna", "rna"]
    assert {c["query"] for c in zen.calls} == set(c1["variants"])
    assert all(c["filters"] == {"published_after": 2000} for c in zen.calls)
    note = (
        "dandi cannot parse boolean queries, so the ontology expansion "
        "(organism/disease/tissue/chemical/assay) was NOT applied there: they were "
        "searched with the plain query"
    )
    assert (p1.errors["query_syntax"], p2.errors["query_syntax"]) == (note, note)


# --- the multi-query first page -------------------------------------------------------


async def test_a_multi_query_first_page_folds_mirrors_and_echoes_every_expansion(
    monkeypatch,
) -> None:
    _resolve_every_facet(monkeypatch)
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    same = {"title": "Mirror Set", "creators": [{"name": "Jane Smith"}]}  # title+surname+year
    mirrors = [
        _rec("zenodo:1", doi="10.5281/zenodo.1", **same),
        _rec("datacite:10.6084/m9.figshare.2", doi="10.6084/m9.figshare.2", **same),
    ]
    seen: list[str] = []

    async def search(client, query, *, size, offset=0, **kw):
        seen.append(query)
        return 2, list(mirrors) if offset == 0 else []

    monkeypatch.setattr(router._ADAPTERS["zenodo"], "search", search)
    async with _client() as client:
        page = await router.search_page(
            client,
            query="rna",
            sources=["zenodo"],
            collapse_mirrors=True,
            multi_query=True,
            **_FACETS,
        )
    assert [(r.id, [m.id for m in r.mirrors]) for r in page.results] == [
        ("zenodo:1", ["datacite:10.6084/m9.figshare.2"])
    ]
    # Every variant, not only variant 0, was ANDed with every facet's group.
    assert len(seen) == 2 and all(g in q for q in seen for g in _GROUPS)
    echoes = (
        page.taxon_expansion,
        page.mesh_expansion,
        page.tissue_expansion,
        page.chemical_expansion,
        page.assay_expansion,
    )
    assert all(e is not None for e in echoes)


async def test_a_multi_query_first_page_reports_a_facet_that_matched_nothing(monkeypatch) -> None:
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    monkeypatch.setattr(assay, "resolve_edam", AsyncMock(return_value=None))
    _serve(monkeypatch, "zenodo", total=0)
    async with _client() as client:
        page = await router.search_page(
            client, query="rna", sources=["zenodo"], assay="alignment", multi_query=True
        )
    assert [(u.field, u.input) for u in page.unresolved] == [("assay", "alignment")]


async def test_a_failed_lookup_is_recorded_for_every_multi_query_variant_not_raised(
    monkeypatch,
) -> None:
    down = AsyncMock(side_effect=httpx.ConnectError("down"))
    for module, name in (
        (taxonomy, "resolve_taxon"),
        (mesh, "resolve_mesh"),
        (anatomy, "resolve_uberon"),
        (chemistry, "resolve_chebi"),
        (assay, "resolve_edam"),
    ):
        monkeypatch.setattr(module, name, down)
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    up = _serve(monkeypatch, "zenodo", total=0)
    async with _client() as client:
        page = await router.search_page(
            client, query="rna", sources=["zenodo"], multi_query=True, **_FACETS
        )
    assert sorted(c["query"] for c in up.calls) == ["alt", "rna"]  # both ran unexpanded
    assert down.await_count == 10  # five lookups for each of the two variants
    keys = ("taxonomy", "mesh", "uberon", "chebi", "edam")
    assert {k: page.errors[k] for k in keys} == dict.fromkeys(keys, "ConnectError: down")


# --- one client for every upstream call ----------------------------------------------


async def test_the_callers_client_reaches_every_upstream_call(monkeypatch) -> None:
    """The client carries the caller's transport, timeouts and connection limits. A call
    made with any other client (or None) skips them, and no fake upstream noticed."""
    router._RESOLVE_CACHE.clear()
    seen: list[tuple[str, object]] = []

    def recording(what: str, value):
        async def call(client, *args, **kwargs):
            seen.append((what, client))
            return value

        return call

    monkeypatch.setattr(taxonomy, "resolve_taxon", recording("taxonomy", _PLANT))
    monkeypatch.setattr(
        mesh, "resolve_mesh", recording("mesh", mesh.MeshInfo(ui="D1", canonical="x", synonyms=()))
    )
    monkeypatch.setattr(
        anatomy,
        "resolve_uberon",
        recording("uberon", anatomy.UberonInfo(uberon_id="U:1", canonical="x", synonyms=())),
    )
    monkeypatch.setattr(
        chemistry,
        "resolve_chebi",
        recording("chebi", chemistry.ChebiInfo(chebi_id="C:1", canonical="x", synonyms=())),
    )
    monkeypatch.setattr(
        assay,
        "resolve_edam",
        recording("edam", assay.EdamInfo(edam_id="E:1", canonical="x", synonyms=())),
    )
    rewrite = recording("rewrite", ParsedRewrite(keyword_core="rna core"))
    monkeypatch.setattr(router.query_understanding_mod, "rewrite", rewrite)
    monkeypatch.setattr(router.query_understanding_mod, "expand", recording("expand", ["alt"]))

    async def rerank(client, query, resources):
        seen.append(("rerank", client))
        return resources, None

    monkeypatch.setattr(embeddings, "rerank", rerank)
    for name in ("zenodo", "dandi"):
        up = _Upstream(name, make=lambda q, i, n=name: _rec(f"{n}:{i}", organism=["Zea mays"]))

        async def search(client, query, *, size, offset=0, _up=up, **kw):
            seen.append((_up.name, client))
            return await _up.search(client, query, size=size, offset=offset, **kw)

        monkeypatch.setattr(router._ADAPTERS[name], "search", search)
    monkeypatch.setattr(
        router.zenodo,
        "resolve",
        recording("zenodo.resolve", _rec("zenodo:7", organism=["Zea mays"])),
    )

    async with _client() as client:
        kw = {"query": "rna", "size": 2, "sources": ["zenodo", "dandi"], "understand": True}
        single = await router.search_page(client, rank="semantic", **kw, **_FACETS)
        multi = await router.search_page(client, multi_query=True, **kw, **_FACETS)
        await router.search_page(client, cursor=single.next_cursor)
        await router.search_page(client, cursor=multi.next_cursor)
        await router.resolve(client, "zenodo:7")
        router._RESOLVE_CACHE.clear()
        await router.relate(client, ["zenodo:7", "zenodo:8"])
    router._RESOLVE_CACHE.clear()
    whats = Counter(what for what, _ in seen)
    assert set(whats) == {
        "taxonomy",
        "mesh",
        "uberon",
        "chebi",
        "edam",
        "rewrite",
        "expand",
        "rerank",
        "zenodo",
        "dandi",
        "zenodo.resolve",
    }
    # Variant 1 of the multi-query search ran every lookup again, with the same client.
    assert all(whats[k] >= 3 for k in ("mesh", "uberon", "chebi", "edam"))
    assert [what for what, c in seen if c is not client] == []


# --- understand -----------------------------------------------------------------------


async def test_understand_echoes_every_extracted_field_and_which_year_the_caller_won(
    monkeypatch,
) -> None:
    parsed = ParsedRewrite(
        keyword_core="maize rna",
        organism="Zea mays",
        disease="blight",
        tissue="leaf",
        chemical="auxin",
        assay="RNA-seq",
        kind="dataset",
        year_min=2010,
        year_max=2020,
        confidence=0.7,
    )
    rewrite = AsyncMock(return_value=parsed)
    monkeypatch.setattr(router.query_understanding_mod, "rewrite", rewrite)
    up = _serve(monkeypatch, "zenodo", total=0)
    async with _client() as client:
        after = await router.search_page(
            client,
            query="find maize rna",
            sources=["zenodo"],
            understand=True,
            published_after=2015,
        )
        before = await router.search_page(
            client,
            query="find maize rna",
            sources=["zenodo"],
            understand=True,
            published_before=2018,
        )
    assert [c.args for c in rewrite.await_args_list] == [(client, "find maize rna")] * 2
    extracted = {
        "keyword_core": "maize rna",
        "organism": "Zea mays",
        "disease": "blight",
        "tissue": "leaf",
        "chemical": "auxin",
        "assay": "RNA-seq",
        "kind": "dataset",
        "year_min": 2010,
        "year_max": 2020,
    }
    echo = {"input": "find maize rna", "keyword_core": "maize rna", "extracted": extracted}
    assert after.query_understanding == QueryUnderstanding(
        **echo,
        applied={"keyword_core": "maize rna", "year_max": 2020},
        overridden=["year_min"],
        confidence=0.7,
    )
    assert before.query_understanding == QueryUnderstanding(
        **echo,
        applied={"keyword_core": "maize rna", "year_min": 2010},
        overridden=["year_max"],
        confidence=0.7,
    )
    # The year each search applied is the one sent upstream; the caller's always wins.
    assert [(c["query"], c["filters"]) for c in up.calls] == [
        ("maize rna", {"published_after": 2015, "published_before": 2020}),
        ("maize rna", {"published_after": 2010, "published_before": 2018}),
    ]


async def test_an_unavailable_llm_is_named_in_errors_for_understand_and_multi_query(
    monkeypatch,
) -> None:
    monkeypatch.setattr(router.query_understanding_mod, "rewrite", AsyncMock(return_value=None))
    expand = AsyncMock(return_value=None)
    monkeypatch.setattr(router.query_understanding_mod, "expand", expand)
    up = _serve(monkeypatch, "zenodo", total=0)
    async with _client() as client:
        page = await router.search_page(
            client, query="rna", sources=["zenodo"], understand=True, multi_query=True
        )
    assert page.errors == {
        "understand": "query understanding unavailable (no LLM endpoint configured or rewrite failed)",
        "multi_query": "multi-query expansion unavailable (no LLM endpoint configured or expansion failed)",
    }
    assert expand.await_args.args[1:] == ("rna",)
    assert expand.await_args.kwargs == {"n": router.MAX_QUERY_VARIANTS} == {"n": 4}
    assert [c["query"] for c in up.calls] == ["rna"]  # fell back to the single query


# --- paging accounting ----------------------------------------------------------------


async def test_a_failed_stream_adds_nothing_to_total_and_does_not_keep_the_walk_open(
    monkeypatch,
) -> None:
    _serve(monkeypatch, "zenodo", total=2)

    async def down(client, query, *, size, offset=0, **kw):
        raise UpstreamUnavailableError("DataCite HTTP 503")

    monkeypatch.setattr(router._ADAPTERS["datacite"], "search", down)
    async with _client() as client:
        page = await router.search_page(client, query="q", size=10, sources=["zenodo", "datacite"])
    assert (page.total, page.count, page.next_cursor) == (2, 2, None)
    assert page.errors == {
        "datacite": "UpstreamUnavailableError: [UpstreamUnavailableError] DataCite HTTP 503"
    }


async def test_a_record_two_variants_both_return_is_emitted_once_and_advances_both(
    monkeypatch,
) -> None:
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)  # no re-rank: interleaved order
    _serve(
        monkeypatch,
        "zenodo",
        make=lambda q, i: _rec("zenodo:shared") if i == 0 else _rec(f"zenodo:{q}:{i}"),
    )
    async with _client() as client:
        page = await router.search_page(
            client, query="rna", size=3, sources=["zenodo"], multi_query=True
        )
    assert [r.id for r in page.results] == ["zenodo:shared", "zenodo:rna:1", "zenodo:alt:1"]
    # Variant 1's copy of the shared record was not emitted but was handled: both
    # streams move past position 0 and 1, with nothing left to skip.
    state = _cursor.decode(page.next_cursor)
    assert (state["offsets"], state.get("ahead")) == ({"0:zenodo": 2, "1:zenodo": 2}, {})


async def test_a_doi_loser_waits_for_its_winner_so_a_failed_winner_stream_loses_nothing(
    monkeypatch,
) -> None:
    """A record that lost DOI dedup is only passed over once its winner is returned. If
    the winner's stream then fails, the loser is the only copy left and is returned."""
    zen = {0: _rec("zenodo:a"), 1: _rec("zenodo:p", doi="10.1/d")}
    dc = {0: _rec("datacite:b"), 1: _rec("datacite:q", doi="10.1/d")}
    _serve(monkeypatch, "datacite", total=2, make=lambda q, i: dc[i])

    async def zenodo(client, query, *, size, offset=0, **kw):
        if offset:
            raise UpstreamUnavailableError("Zenodo HTTP 503")
        return 2, [zen[0], zen[1]][:size]

    monkeypatch.setattr(router._ADAPTERS["zenodo"], "search", zenodo)
    async with _client() as client:
        p1 = await router.search_page(client, query="q", size=2, sources=["zenodo", "datacite"])
        assert _cursor.decode(p1.next_cursor)["offsets"] == {"zenodo": 1, "datacite": 1}
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    assert [r.id for r in p1.results] == ["zenodo:a", "datacite:b"]
    assert [r.id for r in p2.results] == ["datacite:q"]
    assert "zenodo" in p2.errors


async def test_a_reranked_walk_skips_exactly_the_positions_it_already_returned(
    monkeypatch,
) -> None:
    """Re-ranking returns zenodo's third record before its second: the offset stops at
    the second, and the third is kept as a skip one position past the new offset."""
    zen = _serve(monkeypatch, "zenodo", total=5, make=lambda q, i: _rec(f"zenodo:{i}"))
    _serve(monkeypatch, "datacite", total=3, make=lambda q, i: _rec(f"datacite:{i}"))
    first = {"zenodo:0": 0, "zenodo:2": 1, "datacite:0": 2}

    async def rerank(client, query, resources):
        return sorted(resources, key=lambda r: first.get(r.id, 9)), None

    monkeypatch.setattr(embeddings, "rerank", rerank)
    ids: list[str] = []
    async with _client() as client:
        page = await router.search_page(
            client, query="q", size=3, sources=["zenodo", "datacite"], rank="semantic"
        )
        state = _cursor.decode(page.next_cursor)
        assert [r.id for r in page.results] == ["zenodo:0", "zenodo:2", "datacite:0"]
        assert (state["offsets"], state["ahead"]) == ({"zenodo": 1, "datacite": 1}, {"zenodo": [1]})
        ids += [r.id for r in page.results]
        for _ in range(10):
            if page.next_cursor is None:
                break
            page = await router.search_page(client, cursor=page.next_cursor)
            ids += [r.id for r in page.results]
    assert page.next_cursor is None
    assert sorted(ids) == sorted(
        [f"zenodo:{i}" for i in range(5)] + [f"datacite:{i}" for i in range(3)]
    )
    assert zen.calls[1]["offset"] == 1


def test_a_stream_offset_falls_back_to_the_composite_offset_an_old_cursor_stored() -> None:
    """Before omics was split into sub-streams a cursor stored one ``omics`` offset."""
    offsets = {"omics": 7, "omics/sra": 3, "0:omics": 4}
    assert router._stored_offset(offsets, "omics/sra") == 3  # its own offset wins
    assert router._stored_offset(offsets, "omics/geo") == 7
    assert router._stored_offset(offsets, "0:omics/geo") == 4  # multi-query key
    assert router._stored_offset(offsets, "zenodo") == 0
    assert router._stored_offset(offsets, "literature/pubmed") == 0


# --- notes and messages ---------------------------------------------------------------


def test_the_filters_note_names_each_post_filtered_source_once_with_its_summed_count() -> None:
    def stream(key: str, source: str, post_filtered: bool = True) -> router._Stream:
        return router._Stream(
            key=key, label=key, call=AsyncMock(), source=source, post_filtered=post_filtered
        )

    streams = [
        stream("0:gbif", "gbif"),
        stream("1:gbif", "gbif"),
        stream("0:dandi", "dandi"),
        stream("0:zenodo", "zenodo", post_filtered=False),
    ]
    removed = {"0:gbif": 2, "1:gbif": 3, "0:dandi": 1, "0:zenodo": 4}
    filters = {"published_after": 2000, "published_before": None, "kind": "dataset"}
    head = (
        "filters (published_after=2000, kind=dataset) could not be sent upstream to gbif, "
        "dandi, so they were applied after fetch and removed 6 fetched record(s) from this "
        "page (gbif: 5, dandi: 1). `total` counts gbif, dandi UNFILTERED; "
    )
    assert router._filters_note(streams, removed, filters, more=True) == head + (
        "a short or empty page does not mean no matches: next_cursor continues past the "
        "removed records"
    )
    assert router._filters_note(streams, removed, filters, more=False) == (
        head + "this is the last page"
    )
    # A stream that pushed every filter upstream has a filtered total: nothing to say.
    assert router._filters_note(streams, {"0:zenodo": 4}, filters, more=True) is None


async def test_rejections_say_what_was_wrong(monkeypatch) -> None:
    _serve(monkeypatch, "zenodo", total=0)

    async def resolve(client, rid):
        return _rec(rid)

    monkeypatch.setattr(router, "resolve", resolve)
    async with _client() as client:
        with pytest.raises(ValidationError) as kind:
            await router.search_page(client, query="q", kind="nonsense")
        with pytest.raises(ValidationError) as neither:
            await router.search_page(client)
        with pytest.raises(ValidationError) as no_sources:
            await router.search_page(client, query="q", sources=[])
        with pytest.raises(ValueError) as unknown_source:
            await router.search_page(client, query="q", sources=["zenodo", "nope"])
        with pytest.raises(ValidationError) as one:
            await router.relate(client, ["zenodo:1"])
        with pytest.raises(ValidationError) as eleven:
            await router.relate(client, [f"zenodo:{i}" for i in range(11)])
        # Positive controls: the same calls within bounds go through.
        ok = await router.search_page(client, query="q", kind="dataset", sources=["zenodo"])
        ten = await router.relate(client, [f"zenodo:{i}" for i in range(10)])
    names = ", ".join(router._ADAPTERS)
    assert str(kind.value) == (
        "[ValidationError] unknown kind 'nonsense'; valid: "
        "['dataset', 'publication', 'sequencing_run', 'software', 'study']"
    )
    assert str(neither.value) == "[ValidationError] search requires either 'query' or 'cursor'"
    assert str(no_sources.value) == (
        f"[ValidationError] sources must name at least one source (or be omitted for all): {names}"
    )
    assert str(unknown_source.value) == f"unknown source 'nope'; available: {names}"
    assert str(one.value) == "[ValidationError] relate needs at least 2 ids"
    assert str(eleven.value) == "[ValidationError] relate accepts at most 10 ids; got 11"
    assert ok.errors == {}
    assert len(ten.resolved) == 10


async def test_an_unroutable_id_lists_every_accepted_form() -> None:
    async with _client() as client:
        with pytest.raises(ValueError) as unroutable:
            await router.resolve(client, "  nope  ")
    assert str(unroutable.value) == (
        "cannot route id '  nope  ': expected 'zenodo:<id>', 'datacite:<doi>', "
        "'geo:/sra:/bioproject:<acc>', 'pubmed:/openaire:<id>', 'dataone:<pid>', "
        "'gbif:<dataset-key>', 'datagov:<name-slug>', 'nasacmr:<concept-id>', "
        "'omicsdi:<source>:<acc>', 'dandi:<id>', 'cellxgene:<id>', 'openml:<id>', "
        "'pdb:<id>', 'uniprot:<acc>', 'gwas:<acc>', 'biostudies:<acc>', "
        "a bare Zenodo id, or a DOI"
    )


async def test_relate_names_each_failure_by_type_and_says_when_nothing_links(monkeypatch) -> None:
    async def resolve(client, rid):
        if rid == "bad:1":
            raise LookupError("not found")
        return _rec(rid)

    monkeypatch.setattr(router, "resolve", resolve)
    async with _client() as client:
        out = await router.relate(client, ["zenodo:1", "zenodo:2", "bad:1"])
    assert out.errors == {"bad:1": "LookupError: not found"}
    assert out.resolved == ["zenodo:1", "zenodo:2"]
    assert (out.hints, out.note) == ([], "no structural relationships detected among 2 resources")


# --- enrichment -----------------------------------------------------------------------


async def test_an_enrichment_failure_is_named_by_type_on_search_and_logged_on_resolve(
    monkeypatch, caplog
) -> None:
    router._RESOLVE_CACHE.clear()
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(side_effect=RuntimeError("ncbi down")))
    _serve(
        monkeypatch, "zenodo", total=1, make=lambda q, i: _rec("zenodo:5", organism=["Zea mays"])
    )
    monkeypatch.setattr(
        router.zenodo, "resolve", AsyncMock(return_value=_rec("zenodo:5", organism=["Zea mays"]))
    )
    async with _client() as client:
        page = await router.search_page(client, query="q", sources=["zenodo"])
        with caplog.at_level(logging.WARNING, logger=router.__name__):
            resolved = await router.resolve(client, "zenodo:5")
    router._RESOLVE_CACHE.clear()
    assert page.errors == {"taxonomy": "RuntimeError: ncbi down"}
    assert [r.id for r in page.results] == ["zenodo:5"]  # returned un-enriched
    assert resolved.errors == {"taxonomy": "RuntimeError: ncbi down"}
    assert [r.getMessage() for r in caplog.records if r.name == router.__name__] == [
        "resolve enrichment failed for zenodo:5: RuntimeError('ncbi down')"
    ]


async def test_enrichment_skips_a_name_that_resolves_to_nothing_and_leaves_a_known_record_as_is(
    monkeypatch,
) -> None:
    known = {"Zea mays": _PLANT, "nonsense": None}

    async def resolve_taxon(client, name):
        return known[name]

    monkeypatch.setattr(taxonomy, "resolve_taxon", resolve_taxon)
    link = Link(rel="described_in", target_id="plant-genomics:taxid:4577")
    mixed = _rec("zenodo:1", organism=["nonsense", "Zea mays"])
    out = await router._enrich_resource(None, mixed)
    assert [(t.taxid, t.name) for t in out.taxa] == [(4577, "Zea mays")]
    assert out.links == [link]
    unknown = _rec("zenodo:2", organism=["nonsense"])
    assert await router._enrich_resource(None, unknown) is unknown
    done = _rec(
        "zenodo:3", organism=["Zea mays"], taxa=[Taxon(taxid=4577, name="Zea mays")], links=[link]
    )
    assert await router._enrich_resource(None, done) is done


def test_version_status_marks_a_superseded_record_and_keeps_an_adapters_latest_flag() -> None:
    latest = _rec("zenodo:1", is_latest=True)
    assert router._with_version_status(latest) is latest
    old = _rec(
        "zenodo:1",
        is_latest=True,
        links=[Link(rel="is_previous_version_of", target_id="zenodo:2")],
    )
    out = router._with_version_status(old)
    assert (out.is_latest, out.superseded_by) == (False, "zenodo:2")


def test_cache_ttl_reads_the_environment_and_falls_back_to_an_hour(monkeypatch) -> None:
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    assert router._cache_ttl() == 3600.0
    monkeypatch.setenv("CACHE_TTL_SECONDS", "12.5")
    assert router._cache_ttl() == 12.5
    monkeypatch.setenv("CACHE_TTL_SECONDS", "soon")
    assert router._cache_ttl() == 3600.0
