# tests/test_hf_datasets_server.py
import logging

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
