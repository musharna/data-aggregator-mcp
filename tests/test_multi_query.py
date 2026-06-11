from __future__ import annotations

from unittest.mock import AsyncMock

import httpx

from data_aggregator_mcp import _cursor, embeddings, llm, query_understanding, router
from data_aggregator_mcp.models import DataResource

# ---------------------------------------------------------------------------
# query_understanding.expand
# ---------------------------------------------------------------------------


def _enable_llm(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")


async def test_expand_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    called = AsyncMock()
    monkeypatch.setattr(llm, "complete_json", called)
    async with httpx.AsyncClient() as client:
        assert await query_understanding.expand(client, "q", n=4) is None
    called.assert_not_awaited()  # never calls the LLM when disabled


async def test_expand_parses_variants_list(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"variants": ["maize transcriptomics", "Zea mays RNA decay"]}),
    )
    async with httpx.AsyncClient() as client:
        out = await query_understanding.expand(client, "maize rna", n=4)
    assert out == ["maize transcriptomics", "Zea mays RNA decay"]


async def test_expand_filters_garbage_entries(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"variants": ["good one", "", 42, None, "  ", "another"]}),
    )
    async with httpx.AsyncClient() as client:
        out = await query_understanding.expand(client, "q", n=4)
    assert out == ["good one", "another"]  # non-strings + empties dropped


async def test_expand_none_when_no_usable_variants(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(llm, "complete_json", AsyncMock(return_value={"variants": ["", "   ", 7]}))
    async with httpx.AsyncClient() as client:
        assert await query_understanding.expand(client, "q", n=4) is None


async def test_expand_none_when_variants_not_a_list(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(llm, "complete_json", AsyncMock(return_value={"variants": "a, b, c"}))
    async with httpx.AsyncClient() as client:
        assert await query_understanding.expand(client, "q", n=4) is None


async def test_expand_none_when_llm_returns_none(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(llm, "complete_json", AsyncMock(return_value=None))
    async with httpx.AsyncClient() as client:
        assert await query_understanding.expand(client, "q", n=4) is None


async def test_expand_never_raises(monkeypatch) -> None:
    _enable_llm(monkeypatch)
    # complete_json itself never raises, but assert expand tolerates a missing key.
    monkeypatch.setattr(llm, "complete_json", AsyncMock(return_value={"unexpected": 1}))
    async with httpx.AsyncClient() as client:
        assert await query_understanding.expand(client, "q", n=4) is None


# ---------------------------------------------------------------------------
# router multi-query path
# ---------------------------------------------------------------------------


def _res(id_: str, source: str, doi: str | None = None, title: str = "t") -> DataResource:
    return DataResource(id=id_, source=source, kind="dataset", title=title, doi=doi)


def _mock_expand(monkeypatch, variants: list[str] | None) -> AsyncMock:
    m = AsyncMock(return_value=variants)
    monkeypatch.setattr(router.query_understanding_mod, "expand", m)
    return m


def _no_embeddings(monkeypatch) -> None:
    """Force rerank into its degrade path (no endpoint) so order is interleaved."""
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)


async def test_multi_query_fans_out_all_variant_source_pairs(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt one", "alt two"])  # → 3 variants incl. original
    seen_queries: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        seen_queries.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], multi_query=True
        )
    # 3 variants × 1 source = 3 upstream calls, original first.
    assert seen_queries == ["orig", "alt one", "alt two"]
    assert result.query_expansion is not None
    assert result.query_expansion.input == "orig"
    assert result.query_expansion.variants == ["orig", "alt one", "alt two"]


async def test_multi_query_variant_assembly_ci_dedup_and_cap(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    # Original "RNA", plus a ci-duplicate "rna" and 5 distinct → cap to 4 incl. original.
    _mock_expand(monkeypatch, ["rna", "a", "b", "c", "d", "e"])
    seen: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        seen.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(client, query="RNA", sources=["zenodo"], multi_query=True)
    # "rna" is a ci-dup of "RNA" → dropped; capped at MAX_QUERY_VARIANTS (4 incl. original).
    assert result.query_expansion is not None
    assert result.query_expansion.variants == ["RNA", "a", "b", "c"]
    assert len(result.query_expansion.variants) == router.MAX_QUERY_VARIANTS
    assert seen == ["RNA", "a", "b", "c"]


async def test_multi_query_cross_variant_dedup(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt"])  # 2 variants

    # Both variants return a record with the SAME doi → must dedup to ONE.
    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 1, [_res(f"zenodo:{query}", "zenodo", doi="10.x/shared")]

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], size=10, multi_query=True
        )
    # Same DOI from two variants → exactly one record in the output.
    dois = [r.doi for r in result.results]
    assert dois.count("10.x/shared") == 1
    assert len(result.results) == 1
    # total reflects the COMPOSITE sum (1 per stream × 2 streams).
    assert result.total == 2


async def test_multi_query_rerank_anchors_on_original_query(monkeypatch) -> None:
    _mock_expand(monkeypatch, ["expanded variant text"])
    recorded: dict[str, str] = {}

    async def fake_rerank(client, query, resources):
        recorded["query"] = query
        return resources, None

    monkeypatch.setattr(embeddings, "rerank", fake_rerank)

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 1, [_res(f"zenodo:{query}", "zenodo")]

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        await router.search_page(
            client, query="the original user query", sources=["zenodo"], multi_query=True
        )
    # rerank anchored on the ORIGINAL query, not a variant/expanded string.
    assert recorded["query"] == "the original user query"


async def test_multi_query_no_embeddings_degrades_with_results(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt"])

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 1, [_res(f"zenodo:{query}", "zenodo")]

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], multi_query=True
        )
    # No embedding endpoint → semantic note set, but results still returned (recall win).
    assert "semantic" in result.errors
    assert len(result.results) >= 1


async def test_multi_query_fail_soft_falls_back_to_single_query(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, None)  # expansion unavailable
    seen: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        seen.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], multi_query=True
        )
    # Fell back to a single-query search (variant 0 only).
    assert seen == ["orig"]
    assert result.query_expansion is None
    assert "multi_query" in result.errors


async def test_multi_query_false_is_byte_identical(monkeypatch) -> None:
    # multi_query off must NOT touch expand and must produce no query_expansion.
    called = _mock_expand(monkeypatch, ["should", "not", "fire"])

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        on_default = await router.search_page(client, query="orig", sources=["zenodo"])
        off = await router.search_page(client, query="orig", sources=["zenodo"], multi_query=False)
    called.assert_not_awaited()
    assert on_default.query_expansion is None
    assert off.query_expansion is None
    assert on_default.model_dump() == off.model_dump()


# --- pagination -------------------------------------------------------------


async def test_multi_query_pagination_carries_variants_and_offsets(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    expand = _mock_expand(monkeypatch, ["alt"])  # 2 variants

    # Each variant×source returns `size` distinct records, total far ahead → more=True.
    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        recs = [_res(f"zenodo:{query}:{offset + i}", "zenodo") for i in range(size)]
        return 1000, recs

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        page1 = await router.search_page(
            client, query="orig", sources=["zenodo"], size=2, multi_query=True
        )
        assert page1.next_cursor is not None
        decoded = _cursor.decode(page1.next_cursor)
        # Cursor carries the EXPANDED variants and composite offsets.
        assert decoded["variants"] == ["orig", "alt"]
        assert set(decoded["offsets"]) == {"0:zenodo", "1:zenodo"}
        page1_ids = {r.id for r in page1.results}

        # Page 2: continuation must NOT re-call the LLM.
        expand.reset_mock()
        page2 = await router.search_page(client, cursor=page1.next_cursor)
    expand.assert_not_awaited()  # frozen variants — no re-expand
    page2_ids = {r.id for r in page2.results}
    # No overlap between page 1 and page 2.
    assert page1_ids.isdisjoint(page2_ids)
    # query_expansion echo is page-1 only (None on continuation).
    assert page2.query_expansion is None


async def test_multi_query_empty_window_does_not_replay(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt"])

    # total>0 but every stream returns [] → empty window must NOT emit a cursor.
    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 5, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], multi_query=True
        )
    assert result.next_cursor is None  # no replaying cursor on an empty window


async def test_multi_query_termination_when_exhausted(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt"])

    # Each stream returns 1 record and reports total=1 → fully consumed → more=False.
    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        if offset > 0:
            return 1, []
        return 1, [_res(f"zenodo:{query}", "zenodo")]

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], size=10, multi_query=True
        )
    assert result.next_cursor is None


# --- compose with understand ------------------------------------------------


async def test_multi_query_composes_with_understand(monkeypatch) -> None:
    _no_embeddings(monkeypatch)
    from data_aggregator_mcp.query_understanding import ParsedRewrite

    monkeypatch.setattr(
        router.query_understanding_mod,
        "rewrite",
        AsyncMock(return_value=ParsedRewrite(keyword_core="structured core")),
    )
    expand = _mock_expand(monkeypatch, ["facet variant"])
    seen: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        seen.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client,
            query="please find the long natural language thing",
            sources=["zenodo"],
            understand=True,
            multi_query=True,
        )
    # understand structured variant 0 → "structured core" is fanned out as variant 0;
    # expand was called with the rewritten query.
    assert seen[0] == "structured core"
    expand.assert_awaited_once()
    assert expand.await_args.args[1] == "structured core"
    # Both echoes populated. The expansion echo's input is the ORIGINAL user query.
    assert result.query_understanding is not None
    assert result.query_understanding.keyword_core == "structured core"
    assert result.query_expansion is not None
    assert result.query_expansion.input == "please find the long natural language thing"
    assert result.query_expansion.variants[0] == "structured core"


async def test_multi_query_surfaces_per_stream_adapter_error(monkeypatch) -> None:
    """An adapter exception on ONE variant×source stream is surfaced (not swallowed),
    keyed per variant so a sibling variant's success for the same source still counts."""
    _no_embeddings(monkeypatch)
    _mock_expand(monkeypatch, ["alt"])  # 2 variants

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        if query == "orig":
            raise RuntimeError("upstream down")
        return 1, [_res("zenodo:ok", "zenodo")]

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", sources=["zenodo"], multi_query=True
        )
    # variant 0 failed → keyed error; variant 1 still returned a record.
    assert "zenodo#v0" in result.errors
    assert "RuntimeError" in result.errors["zenodo#v0"]
    assert any(r.id == "zenodo:ok" for r in result.results)


async def test_multi_query_variant_ontology_expansion_applied(monkeypatch) -> None:
    """Every variant gets the SAME ontology expansion; the echo is captured once."""
    _no_embeddings(monkeypatch)
    from data_aggregator_mcp import taxonomy

    monkeypatch.setattr(
        taxonomy,
        "resolve_taxon",
        AsyncMock(
            return_value=taxonomy.TaxonInfo(
                taxid=4577, canonical_name="Zea mays", synonyms=("maize",), is_plant=True
            )
        ),
    )
    _mock_expand(monkeypatch, ["alt query"])
    seen: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        seen.append(query)
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="orig", organism="Zea mays", sources=["zenodo"], multi_query=True
        )
    # Both variants ANDed with the organism synonym group.
    assert all('"Zea mays" OR "maize"' in q for q in seen)
    assert seen[0].startswith("(orig)")
    assert seen[1].startswith("(alt query)")
    # Ontology echo captured once (on variant 0).
    assert result.taxon_expansion is not None
    assert result.taxon_expansion.taxid == 4577
