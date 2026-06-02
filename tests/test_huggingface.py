import os

import httpx
import pytest

from data_aggregator_mcp import huggingface
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

_DS = {
    "id": "owner/name",
    "author": "owner",
    "createdAt": "2022-06-09T17:34:13.000Z",
    "tags": ["license:mit", "format:parquet", "biology"],
    "gated": False,
}


@pytest.mark.asyncio
async def test_search_normalizes():
    async def handler(request):
        assert request.url.params["search"] == "dna"
        return httpx.Response(200, json=[_DS])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await huggingface.search(c, "dna", size=10)
    assert total == 1
    r = recs[0]
    assert r.id == "hf:owner/name" and r.source == "huggingface" and r.kind == "dataset"
    assert r.creators == [Creator(name="owner")]
    assert r.year == 2022 and r.license == "mit" and r.access == "open"


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[_DS]))
    ) as c:
        total, recs = await huggingface.search(c, "dna", size=10, offset=5)
    assert (total, recs) == (0, [])


@pytest.mark.asyncio
async def test_resolve_attaches_files_skips_gitattributes():
    body = {
        **_DS,
        "siblings": [
            {"rfilename": ".gitattributes"},
            {"rfilename": "data/train.parquet"},
        ],
    }

    async def handler(request):
        if request.url.host == "datasets-server.huggingface.co":
            return httpx.Response(404)
        assert request.url.path.endswith("/api/datasets/owner/name")
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    names = [f.name for f in r.files]
    assert names == ["data/train.parquet"]
    assert (
        r.files[0].url
        == "https://huggingface.co/datasets/owner/name/resolve/main/data/train.parquet"
    )


@pytest.mark.asyncio
async def test_resolve_404():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(NotFoundError):
            await huggingface.resolve(c, "hf:owner/missing")


def test_normalize_no_license_tag():
    assert huggingface._normalize({"id": "a/b", "author": "a", "tags": []}).license is None


def test_normalize_populates_metrics_from_hf_fields():
    d = {"id": "owner/name", "downloads": 1234, "likes": 9, "createdAt": "2024-01-01"}
    r = huggingface._normalize(d)
    assert r.metrics is not None
    assert r.metrics.downloads == 1234
    assert r.metrics.likes == 9
    assert r.metrics.citations is None


def test_normalize_metrics_none_when_hf_counts_absent():
    assert huggingface._normalize({"id": "owner/name"}).metrics is None


def test_normalize_populates_last_updated():
    d = {"id": "owner/name", "lastModified": "2025-06-01T12:00:00.000Z"}
    assert huggingface._normalize(d).last_updated == "2025-06-01T12:00:00.000Z"


@pytest.mark.asyncio
async def test_search_gated_is_restricted():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{**_DS, "gated": True}]))
    ) as c:
        _t, recs = await huggingface.search(c, "x", size=5)
    assert recs[0].access == "restricted"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@pytest.mark.asyncio
async def test_resolve_enriches_with_datasets_server_parquet(monkeypatch):
    from data_aggregator_mcp import hf_datasets_server
    from data_aggregator_mcp.models import FileEntry

    body = {**_DS, "siblings": [{"rfilename": "README.md"}]}

    async def fake_parquet(client, ds_id):
        return [
            FileEntry(
                name="default/train/0000.parquet",
                url="https://h/0.parquet",
                source="hf-datasets-server",
            )
        ]

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    names = [f.name for f in r.files]
    assert names == ["README.md", "default/train/0000.parquet"]
    assert r.files[-1].source == "hf-datasets-server"


@pytest.mark.asyncio
async def test_resolve_survives_datasets_server_404(monkeypatch):
    from data_aggregator_mcp import hf_datasets_server
    from data_aggregator_mcp.errors import NotFoundError as NFE

    body = {**_DS, "siblings": [{"rfilename": "data/train.parquet"}]}

    async def fake_parquet(client, ds_id):
        raise NFE("no converted view")

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    assert [f.name for f in r.files] == ["data/train.parquet"]  # raw siblings only


@pytest.mark.asyncio
async def test_resolve_logs_on_datasets_server_error(monkeypatch, caplog):
    import logging as _logging

    from data_aggregator_mcp import hf_datasets_server

    body = {**_DS, "siblings": [{"rfilename": "data/train.parquet"}]}

    async def fake_parquet(client, ds_id):
        raise RuntimeError("datasets-server 503")

    monkeypatch.setattr(hf_datasets_server, "parquet_files", fake_parquet)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        with caplog.at_level(_logging.WARNING):
            r = await huggingface.resolve(c, "hf:owner/name")
    assert [f.name for f in r.files] == ["data/train.parquet"]  # never breaks resolve
    assert any("datasets-server" in m.lower() for m in caplog.messages)


@_live_only
@pytest.mark.asyncio
async def test_live_search_and_resolve() -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        total, recs = await huggingface.search(client, "dna", size=5)
        assert total >= 1
        r0 = recs[0]
        assert r0.id.startswith("hf:") and r0.source == "huggingface" and r0.kind == "dataset"
        # resolve a known small dataset → files attached with working URLs
        full = await huggingface.resolve(client, "hf:davidcechak/Arabidopsis_thaliana_DNA_v0")
        assert full.files, "resolve should attach siblings as files"
        head = await client.head(full.files[0].url)
        assert head.status_code < 400  # resolve URL serves (2xx/3xx)
