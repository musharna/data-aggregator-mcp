# tests/test_hf_datasets_server.py
import logging
import os

import httpx
import pytest

from data_aggregator_mcp import hf_datasets_server
from data_aggregator_mcp.errors import NotFoundError

_PARQUET_BODY = {
    "parquet_files": [
        {
            "config": "default",
            "split": "test",
            "url": "https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/test/0000.parquet",
            "size": 239511,
        },
        {
            "config": "default",
            "split": "train",
            "url": "https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/train/0000.parquet",
            "size": 1857755,
        },
    ],
    "partial": False,
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_parquet_files_maps_entries():
    async def handler(request):
        assert request.url.host == "datasets-server.huggingface.co"
        assert request.url.path == "/parquet"
        assert request.url.params["dataset"] == "o/n"
        return httpx.Response(200, json=_PARQUET_BODY)

    async with _client(handler) as c:
        files = await hf_datasets_server.parquet_files(c, "o/n")
    assert [f.name for f in files] == [
        "default/test/0000.parquet",
        "default/train/0000.parquet",
    ]
    assert files[0].url == _PARQUET_BODY["parquet_files"][0]["url"]
    assert files[0].size == 239511
    assert all(f.source == "hf-datasets-server" for f in files)


@pytest.mark.asyncio
async def test_parquet_files_empty_list():
    async with _client(lambda r: httpx.Response(200, json={"parquet_files": []})) as c:
        assert await hf_datasets_server.parquet_files(c, "o/n") == []


@pytest.mark.asyncio
async def test_parquet_files_skips_malformed_entries():
    body = {"parquet_files": [{"config": "d", "split": "s"}, {"url": "u"}]}  # each missing a field
    async with _client(lambda r: httpx.Response(200, json=body)) as c:
        assert await hf_datasets_server.parquet_files(c, "o/n") == []


@pytest.mark.asyncio
async def test_parquet_files_caps_and_warns(caplog):
    many = {
        "parquet_files": [
            {"config": "d", "split": "s", "url": f"https://h/x/{i}.parquet", "size": 1}
            for i in range(hf_datasets_server.MAX_DSS_FILES + 5)
        ]
    }
    async with _client(lambda r: httpx.Response(200, json=many)) as c:
        with caplog.at_level(logging.WARNING):
            files = await hf_datasets_server.parquet_files(c, "o/n")
    assert len(files) == hf_datasets_server.MAX_DSS_FILES
    assert any("capping" in m.lower() for m in caplog.messages)


@pytest.mark.asyncio
async def test_parquet_files_404_raises_notfound():
    async with _client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(NotFoundError):
            await hf_datasets_server.parquet_files(c, "o/missing")


def test_converted_parquet_file_advertises_operate_modes():
    from data_aggregator_mcp.models import FileEntry, derive_access_modes

    files = [
        FileEntry(
            name="default/train/0000.parquet",
            url="https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet/default/train/0000.parquet",
            source="hf-datasets-server",
        )
    ]
    assert derive_access_modes(files, operate=True) == [
        "fetch",
        "schema",
        "preview",
        "head",
        "sql",
    ]


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_enriches_and_operates():
    from data_aggregator_mcp import huggingface, operate

    ds = "hf:mteb/tweet_sentiment_extraction"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await huggingface.resolve(c, ds)
        dss = [f for f in r.files if f.source == "hf-datasets-server"]
        assert dss, "resolve should surface datasets-server parquet files"

        sch = await operate.run(c, ds, "schema", file=dss[0].name)
        assert sch["columns"]
        rows = await operate.run(c, ds, "sql", query="SELECT * FROM data LIMIT 5", file=dss[0].name)
        assert len(rows["rows"]) == 5
