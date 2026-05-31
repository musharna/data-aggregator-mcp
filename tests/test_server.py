from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import server
from data_aggregator_mcp.errors import FetchNotSupportedError


def test_catalog_exposes_four_tools() -> None:
    names = {t.name for t in server.TOOLS}
    assert names == {"search", "resolve", "fetch", "list_sources"}


def test_list_sources_reports_zenodo() -> None:
    out = server._SOURCES
    assert any(s["name"] == "zenodo" and s["layer"] == "archives" for s in out)


def test_list_sources_reports_datacite() -> None:
    out = server._SOURCES
    assert any(s["name"] == "datacite" and s["layer"] == "archives" for s in out)


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
    from data_aggregator_mcp.models import DataResource

    async def fake_resolve(client, fid):
        return DataResource(id=fid, source="mendeley", kind="dataset", title="t", doi="10.x/y")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    with pytest.raises(FetchNotSupportedError):
        await server._dispatch("fetch", {"id": "datacite:10.17632/abc"})


async def test_dispatch_list_sources() -> None:
    out = await server._dispatch("list_sources", {})
    assert "sources" in out


def test_all_tool_outputs_validate_against_schemas() -> None:
    # outputSchema is declared per tool; ensure each model still serializes.
    from data_aggregator_mcp.models import DataResource, FetchResult, SearchResult

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
    from data_aggregator_mcp.models import DataResource

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
    from data_aggregator_mcp.models import DataResource

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
    from data_aggregator_mcp.models import DataResource

    async def fake_resolve(client, fid):
        return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    out = await server._dispatch("resolve", {"id": "zenodo:1"})
    assert out["citation"] is None


def test_resolve_tool_exposes_cite_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "resolve")
    assert "cite" in tool.inputSchema["properties"]


def test_list_sources_advertises_filters_and_cursor() -> None:
    for s in server._SOURCES:
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
    from data_aggregator_mcp.models import DataResource, FileEntry

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
