from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import server
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource, FileEntry, Link, TrustSignals


def test_catalog_exposes_core_tools() -> None:
    names = {t.name for t in server.TOOLS}
    assert names == {"search", "resolve", "fetch", "list_sources", "operate"}


def test_list_sources_reports_zenodo() -> None:
    out = server._SOURCES
    assert any(s["name"] == "zenodo" and s["layer"] == "archives" for s in out)


def test_list_sources_reports_datacite() -> None:
    out = server._SOURCES
    assert any(s["name"] == "datacite" and s["layer"] == "archives" for s in out)


def test_hf_is_fetchable() -> None:
    assert server._is_fetchable("hf:owner/name") is True


async def test_list_sources_includes_huggingface() -> None:
    out = await server._dispatch("list_sources", {})
    names = {s["name"] for s in out["sources"]}
    assert "huggingface" in names


def test_search_tool_exposes_sources_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    assert "sources" in tool.inputSchema["properties"]


async def test_dispatch_search_routes_to_zenodo(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=rice&size=10",
        json={"hits": {"total": 0, "hits": []}},
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rice&page%5Bsize%5D=10",
        json={"data": [], "meta": {"total": 0}},
    )
    out = await server._dispatch("search", {"query": "rice", "sources": ["zenodo", "datacite"]})
    assert out["query"] == "rice"
    assert out["total"] == 0
    assert out["results"] == []


async def test_dispatch_search_merges_and_surfaces_errors(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=rice&size=10",
        json={
            "hits": {
                "total": 1,
                "hits": [
                    {
                        "id": 9,
                        "doi": "10.5281/zenodo.9",
                        "metadata": {
                            "title": "z",
                            "resource_type": {"type": "dataset"},
                            "publication_date": "2024-01-01",
                        },
                        "files": [],
                    }
                ],
            }
        },
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rice&page%5Bsize%5D=10",
        status_code=500,
        is_reusable=True,
    )
    out = await server._dispatch("search", {"query": "rice", "sources": ["zenodo", "datacite"]})
    assert out["count"] == 1
    assert "datacite" in out["errors"]


async def test_dispatch_fetch_fails_loud_for_unsupported_datacite_repo(monkeypatch) -> None:

    async def fake_resolve(client, fid):
        return DataResource(id=fid, source="mendeley", kind="dataset", title="t", doi="10.x/y")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    with pytest.raises(FetchNotSupportedError):
        await server._dispatch("fetch", {"id": "datacite:10.17632/abc"})


async def test_dispatch_list_sources() -> None:
    out = await server._dispatch("list_sources", {})
    assert "sources" in out


def test_list_sources_includes_dataone_and_omicsdi():
    by_name = {s["name"]: s for s in server._SOURCES}
    assert "dataone" in by_name and "omicsdi" in by_name
    d1 = by_name["dataone"]
    assert d1["layer"] == "archives" and d1["fetchable"] is True
    assert "md5" in d1["fetchable_notes"].lower() or "sha" in d1["fetchable_notes"].lower()
    od = by_name["omicsdi"]
    assert od["layer"] == "omics" and od["fetchable"] == "per-repo"
    assert "id_example" in d1 and "id_example" in od


def test_all_tool_outputs_validate_against_schemas() -> None:
    # outputSchema is declared per tool; ensure each model still serializes.
    from data_aggregator_mcp.models import FetchResult, SearchResult

    SearchResult(query="q", total=0, count=0).model_dump()
    DataResource(id="datacite:10.x/y", source="dryad", kind="dataset", title="t").model_dump()
    FetchResult().model_dump()


def test_list_sources_reports_omics() -> None:
    out = server._SOURCES
    assert any(s["name"] == "omics" and s["layer"] == "omics" for s in out)


async def test_dispatch_fetch_fails_loud_for_bioproject_id() -> None:
    # BioProject stays files=[] and is NOT allowlisted (discovery-only). SRA is now
    # fetchable, so the gate coverage moves to a still-non-fetchable omics id.
    with pytest.raises(FetchNotSupportedError, match="no wired fetch backend"):
        await server._dispatch("fetch", {"id": "bioproject:PRJNA111"})


async def test_list_sources_reports_literature() -> None:
    out = await server._dispatch("list_sources", {})
    names = {s["name"] for s in out["sources"]}
    assert "literature" in names
    lit = next(s for s in out["sources"] if s["name"] == "literature")
    assert lit["layer"] == "literature"
    assert lit["kinds"] == ["publication"]


async def test_dispatch_fetch_fails_loud_for_paywalled_literature_id(monkeypatch) -> None:
    # Literature ids now pass the cheap pre-gate (OA full text is fetchable), so the
    # fail-loud moves post-resolve: a paywalled / non-OA paper resolves with files=[]
    # and is rejected by _ensure_fulltext_available rather than silently empty.

    async def fake_resolve(client, fid):
        return DataResource(id=fid, source="pubmed", kind="publication", title="t")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    with pytest.raises(FetchNotSupportedError, match="open-access full text"):
        await server._dispatch("fetch", {"id": "pubmed:34320281"})


async def test_list_sources_exposes_fetchable_and_examples() -> None:
    from data_aggregator_mcp import server

    out = await server._dispatch("list_sources", {})
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["zenodo"]["fetchable"] is True
    assert "id_example" in by_name["omics"]
    assert "organism" in by_name["omics"]["filters_supported"]
    # sub-source fetchability is described for the multiplexed sources
    assert "dryad" in by_name["datacite"]["fetchable_notes"].lower()


def test_search_tool_exposes_organism_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    assert "organism" in tool.inputSchema["properties"]


def test_search_tool_exposes_disease_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    assert "disease" in tool.inputSchema["properties"]


async def test_dispatch_search_passes_disease_to_router(monkeypatch) -> None:
    from data_aggregator_mcp.models import MeshExpansion, SearchResult

    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(
            query=kwargs.get("query"),
            total=0,
            count=0,
            results=[],
            errors={},
            mesh_expansion=MeshExpansion(
                input="breast cancer",
                mesh_ui="D001943",
                canonical_name="Breast Neoplasms",
                synonyms=["Breast Cancer"],
            ),
        )

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    out = await server._dispatch("search", {"query": "rna", "disease": "breast cancer"})
    assert captured["disease"] == "breast cancer"
    assert out["mesh_expansion"]["mesh_ui"] == "D001943"


async def test_dispatch_search_passes_organism_to_router(monkeypatch) -> None:
    from data_aggregator_mcp.models import SearchResult

    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query=kwargs.get("query"), total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    out = await server._dispatch("search", {"query": "rna", "organism": "Zea mays"})
    assert captured["organism"] == "Zea mays"
    assert out["taxon_expansion"] is None


async def test_dispatch_resolve_renders_citation_when_cite_given(httpx_mock, monkeypatch) -> None:

    async def fake_resolve(client, fid):
        return DataResource(
            id="datacite:10.1038/x",
            source="datacite",
            kind="publication",
            title="t",
            doi="10.1038/x",
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    httpx_mock.add_response(url="https://doi.org/10.1038/x", text="@article{x}")
    out = await server._dispatch("resolve", {"id": "datacite:10.1038/x", "cite": "bibtex"})
    assert out["citation"] == "@article{x}"


async def test_dispatch_resolve_no_cite_leaves_citation_none(monkeypatch) -> None:

    async def fake_resolve(client, fid):
        return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1"})
    assert out["citation"] is None


def test_resolve_tool_exposes_cite_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "resolve")
    assert "cite" in tool.inputSchema["properties"]


def test_list_sources_advertises_filters_and_cursor() -> None:
    # Sources that support the full temporal+kind+cursor filter set.
    # DataONE and OmicsDI are discovery-limited sources with fewer filters.
    _FULL_FILTER_SOURCES = {"zenodo", "datacite", "omics", "literature", "huggingface"}
    for s in server._SOURCES:
        if s["name"] not in _FULL_FILTER_SOURCES:
            continue
        assert {"published_after", "published_before", "kind", "cursor"} <= set(
            s["filters_supported"]
        )


def test_search_schema_exposes_pagination_and_filters() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    props = tool.inputSchema["properties"]
    assert {"cursor", "published_after", "published_before", "kind"} <= set(props)
    assert props["kind"]["enum"] == [
        "dataset",
        "sequencing_run",
        "study",
        "publication",
        "software",
    ]


def test_search_schema_query_not_required() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    assert "query" not in tool.inputSchema.get("required", [])


async def test_dispatch_threads_cursor(monkeypatch) -> None:
    from data_aggregator_mcp.models import SearchResult

    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query="q", total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    await server._dispatch("search", {"cursor": "tok"})
    assert captured["cursor"] == "tok"
    assert captured["query"] is None


class _FakeSession:
    def __init__(self) -> None:
        self.progress_calls: list[tuple] = []

    async def send_progress_notification(
        self, progress_token, progress, total=None, message=None, **kw
    ):
        self.progress_calls.append((progress_token, progress, total))


class _FakeMeta:
    def __init__(self, token) -> None:
        self.progressToken = token


class _FakeReqCtx:
    def __init__(self, token, session) -> None:
        self.meta = _FakeMeta(token) if token is not None else None
        self.session = session


def _fake_fetchable_resolve():

    async def fake_resolve(client, fid):
        return DataResource(
            id="zenodo:1",
            source="zenodo",
            kind="dataset",
            title="t",
            files=[
                FileEntry(name="a.txt", url="https://x/a.txt", size=1),
                FileEntry(name="b.txt", url="https://x/b.txt", size=1),
            ],
        )

    return fake_resolve


async def test_dispatch_fetch_sends_progress_when_token_present(monkeypatch) -> None:
    import mcp.server.lowlevel.server as low

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", _fake_fetchable_resolve())

    async def fake_fetch_files(client, resource, *, on_progress=None, **kw):
        from data_aggregator_mcp.models import FetchResult

        if on_progress is not None:
            await on_progress(1, 2, "a.txt")
            await on_progress(2, 2, "b.txt")
        return FetchResult(paths=["/tmp/a.txt", "/tmp/b.txt"], bytes=2)

    monkeypatch.setattr("data_aggregator_mcp.fetch.fetch_files", fake_fetch_files)

    sess = _FakeSession()
    tok = low.request_ctx.set(_FakeReqCtx("tok-123", sess))
    try:
        out = await server._dispatch("fetch", {"id": "zenodo:1"})
    finally:
        low.request_ctx.reset(tok)

    assert len(out["paths"]) == 2
    assert len(sess.progress_calls) >= 1
    assert sess.progress_calls[0][0] == "tok-123"


async def test_dispatch_fetch_no_token_sends_nothing(monkeypatch) -> None:
    import mcp.server.lowlevel.server as low

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", _fake_fetchable_resolve())

    async def fake_fetch_files(client, resource, *, on_progress=None, **kw):
        from data_aggregator_mcp.models import FetchResult

        if on_progress is not None:
            await on_progress(1, 1, "a.txt")
        return FetchResult(paths=["/tmp/a.txt"], bytes=1)

    monkeypatch.setattr("data_aggregator_mcp.fetch.fetch_files", fake_fetch_files)

    sess = _FakeSession()
    tok = low.request_ctx.set(_FakeReqCtx(None, sess))
    try:
        out = await server._dispatch("fetch", {"id": "zenodo:1"})
    finally:
        low.request_ctx.reset(tok)

    assert len(out["paths"]) == 1  # fetch still returns normally
    assert sess.progress_calls == []  # no token ⇒ no notifications


async def test_dispatch_threads_filters(monkeypatch) -> None:
    from data_aggregator_mcp.models import SearchResult

    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query="rice", total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    await server._dispatch(
        "search",
        {
            "query": "rice",
            "published_after": 2010,
            "published_before": 2020,
            "kind": "dataset",
        },
    )
    assert captured["published_after"] == 2010
    assert captured["published_before"] == 2020
    assert captured["kind"] == "dataset"


@pytest.mark.asyncio
async def test_list_sources_default_is_network_free():
    out = await server._dispatch("list_sources", {})
    assert "sources" in out
    assert all("health" not in s for s in out["sources"])


@pytest.mark.asyncio
async def test_list_sources_check_health_merges_health(monkeypatch):
    async def fake_probe(client):
        return [
            {"name": n, "status": "up", "latency_ms": 12, "detail": None}
            for n in [s["name"] for s in server._SOURCES]
        ]

    monkeypatch.setattr(server.health_mod, "probe_sources", fake_probe)
    out = await server._dispatch("list_sources", {"check_health": True})
    assert all(s["health"]["status"] == "up" for s in out["sources"])


@pytest.mark.asyncio
async def test_search_dispatch_passes_rank(monkeypatch):  # IRON_LAW_OK
    captured = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        from data_aggregator_mcp.models import SearchResult

        return SearchResult(query=kwargs.get("query"), total=0, count=0, results=[], errors={})

    monkeypatch.setattr(server.router, "search_page", fake_search_page)
    await server._dispatch("search", {"query": "q", "rank": "semantic"})
    assert captured["rank"] == "semantic"


def test_read_only_tools_are_annotated() -> None:
    from data_aggregator_mcp import server

    by_name = {t.name: t for t in server.TOOLS}
    for n in ("search", "resolve", "list_sources"):
        assert by_name[n].annotations is not None
        assert by_name[n].annotations.readOnlyHint is True
    # fetch writes files → not read-only, and not destructive to existing state
    assert by_name["fetch"].annotations.readOnlyHint is False
    assert by_name["fetch"].annotations.destructiveHint is False


async def test_list_prompts_exposes_templates() -> None:
    from data_aggregator_mcp import server

    prompts = await server._list_prompts()
    names = {p.name for p in prompts}
    assert {"find_data", "data_behind_paper", "search_resolve_fetch"} <= names


async def test_get_prompt_find_data_includes_topic() -> None:
    from data_aggregator_mcp import server

    result = await server._get_prompt(
        "find_data", {"topic": "rice drought", "organism": "Oryza sativa"}
    )
    text = result.messages[0].content.text
    assert "rice drought" in text
    assert "Oryza sativa" in text


async def test_dispatch_resolve_renders_croissant(monkeypatch) -> None:

    async def fake_resolve(client, fid):
        return DataResource(
            id="zenodo:1",
            source="zenodo",
            kind="dataset",
            title="t",
            files=[FileEntry(name="a.csv", url="https://x/a.csv")],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1", "format": "croissant"})
    assert out["croissant"]["@type"] == "Dataset"
    assert out["croissant"]["distribution"][0]["name"] == "a.csv"


async def test_dispatch_resolve_renders_ro_crate(monkeypatch) -> None:

    async def fake_resolve(client, fid):
        return DataResource(
            id="zenodo:1",
            source="zenodo",
            kind="dataset",
            title="t",
            files=[FileEntry(name="a.csv", url="https://x/a.csv")],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1", "format": "ro-crate"})
    assert out["ro_crate"]["@context"] == "https://w3id.org/ro/crate/1.1/context"


async def test_dispatch_resolve_attaches_trust_when_requested(monkeypatch) -> None:
    async def fake_resolve(client, fid):
        return DataResource(
            id="pub:1",
            source="literature",
            kind="publication",
            title="t",
            doi="10.1016/S0140-6736(97)11096-0",
        )

    seen = {}

    async def fake_annotate(client, resource):
        seen["doi"] = resource.doi  # prove the resolved resource (with its DOI) reaches annotate
        return TrustSignals(retracted=True, retraction_doi="10.1/notice", concern=False)

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr("data_aggregator_mcp.trust.annotate", fake_annotate)
    out = await server._dispatch("resolve", {"id": "pub:1", "trust": True})
    assert seen["doi"] == "10.1016/S0140-6736(97)11096-0"
    assert out["trust"]["retracted"] is True
    assert out["trust"]["retraction_doi"] == "10.1/notice"
    assert out["trust"]["concern"] is False


async def test_dispatch_resolve_no_trust_leaves_trust_none(monkeypatch) -> None:
    async def fake_resolve(client, fid):
        return DataResource(
            id="pub:1",
            source="literature",
            kind="publication",
            title="t",
            doi="10.1016/S0140-6736(97)11096-0",
        )

    async def boom_annotate(client, resource):
        raise AssertionError("annotate must not be called without trust=True")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr("data_aggregator_mcp.trust.annotate", boom_annotate)
    out = await server._dispatch("resolve", {"id": "pub:1"})
    assert out["trust"] is None


def test_resolve_tool_exposes_trust_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "resolve")
    assert "trust" in tool.inputSchema["properties"]


def test_dataone_and_omicsdi_are_fetchable_prefixes():
    assert server._is_fetchable("dataone:doi:10.5/x")
    assert server._is_fetchable("omicsdi:pride:PXD000001")


def test_ensure_omicsdi_fetchable_raises_when_no_files():
    r = DataResource(id="omicsdi:massive:MSV1", source="omicsdi", kind="study", title="x", files=[])
    with pytest.raises(FetchNotSupportedError):
        server._ensure_omicsdi_fetchable("omicsdi:massive:MSV1", r)


def test_ensure_omicsdi_fetchable_passes_when_files_present():
    r = DataResource(
        id="omicsdi:pride:PXD1",
        source="omicsdi",
        kind="study",
        title="x",
        files=[FileEntry(name="a", url="https://x/a")],
    )
    server._ensure_omicsdi_fetchable("omicsdi:pride:PXD1", r)  # no raise


def test_ensure_omicsdi_fetchable_ignores_non_omicsdi_ids():
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="x", files=[])
    server._ensure_omicsdi_fetchable("zenodo:1", r)  # no raise


def test_ensure_omicsdi_fetchable_error_points_to_landing_page():
    landing = "https://www.omicsdi.org/dataset/massive/MSV1"
    r = DataResource(
        id="omicsdi:massive:MSV1",
        source="omicsdi",
        kind="study",
        title="x",
        files=[],
        links=[Link(rel="landing_page", target_id=landing)],
    )
    with pytest.raises(FetchNotSupportedError) as exc:
        server._ensure_omicsdi_fetchable("omicsdi:massive:MSV1", r)
    assert landing in str(exc.value)  # actionable pointer to the source repo


# ---------------------------------------------------------------------------
# Fix #2 — search tool schema must advertise dataone and omicsdi in sources
# ---------------------------------------------------------------------------


def test_search_schema_sources_description_includes_dataone_and_omicsdi():
    tool = next(t for t in server.TOOLS if t.name == "search")
    sources_desc = tool.inputSchema["properties"]["sources"]["description"]
    assert "dataone" in sources_desc, (
        f"'dataone' missing from sources description: {sources_desc!r}"
    )
    assert "omicsdi" in sources_desc, (
        f"'omicsdi' missing from sources description: {sources_desc!r}"
    )


def test_operate_tool_registered():
    names = {t.name for t in server.TOOLS}
    assert "operate" in names
    op = next(t for t in server.TOOLS if t.name == "operate")
    assert op.inputSchema["required"] == ["op", "id"]
    assert set(op.inputSchema["properties"]["op"]["enum"]) == {"schema", "preview", "head", "sql"}


@pytest.mark.asyncio
async def test_operate_dispatch_routes(monkeypatch):
    async def fake_run(client, rid, op, **kw):
        return {"op": op, "file": "x.parquet", "columns": [], "rows": []}

    monkeypatch.setattr(server.operate, "run", fake_run)
    out = await server._dispatch("operate", {"id": "zenodo:1", "op": "schema"})
    assert out["op"] == "schema"


@pytest.mark.asyncio
async def test_list_sources_advertises_operable():
    out = await server._dispatch("list_sources", {})
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["zenodo"].get("operable") is True
    assert by_name["datacite"].get("operable") is True
    assert by_name["huggingface"].get("operable") is True
    assert by_name["dataone"].get("operable") is True
    # discovery-only / non-tabular sources stay false/absent
    assert by_name["omicsdi"].get("operable") in (False, None)
    assert by_name["omics"].get("operable") in (False, None)
    assert by_name["literature"].get("operable") in (False, None)


# ---------------------------------------------------------------------------
# MCP resources primitive (P4.2) — list/read resource handlers
# ---------------------------------------------------------------------------


async def test_list_resources_returns_catalog() -> None:
    from data_aggregator_mcp import resources

    res = await server._list_resources()
    assert [str(r.uri) for r in res] == [resources.CATALOG_URI]


async def test_list_resource_templates_returns_record_template() -> None:
    tmpls = await server._list_resource_templates()
    assert tmpls[0].uriTemplate == "dataresource://record/{id}"


async def test_read_resource_catalog_returns_sources_json() -> None:
    import json

    from pydantic import AnyUrl

    from data_aggregator_mcp import resources

    contents = await server._read_resource(AnyUrl(resources.CATALOG_URI))
    item = list(contents)[0]
    assert item.mime_type == "application/json"
    payload = json.loads(item.content)
    assert "sources" in payload and any(s["name"] == "zenodo" for s in payload["sources"])


async def test_read_resource_record_routes_through_resolve(monkeypatch) -> None:
    import json

    from pydantic import AnyUrl

    from data_aggregator_mcp import resources

    async def fake_resolve(client, rid):
        assert rid == "datacite:10.5061/dryad.x"  # decoded from the URI path
        return DataResource(id=rid, source="datacite", kind="dataset", title="Probe set")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    uri = AnyUrl(resources.record_uri("datacite:10.5061/dryad.x"))
    contents = await server._read_resource(uri)
    item = list(contents)[0]
    assert item.mime_type == "application/json"
    assert json.loads(item.content)["id"] == "datacite:10.5061/dryad.x"


async def test_read_resource_unknown_uri_raises() -> None:
    from pydantic import AnyUrl

    with pytest.raises(ValueError):
        await server._read_resource(AnyUrl("dataresource://nope/x"))


async def test_read_resource_record_propagates_not_found(monkeypatch) -> None:
    # a valid record URI whose id resolves to nothing must surface NotFoundError
    # (fail loud), not swallow it into an empty/garbage resource.
    from pydantic import AnyUrl

    from data_aggregator_mcp import resources
    from data_aggregator_mcp.errors import NotFoundError

    async def fake_resolve(client, rid):
        raise NotFoundError(f"no such record: {rid!r}")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    with pytest.raises(NotFoundError):
        await server._read_resource(AnyUrl(resources.record_uri("pdb:9999")))
