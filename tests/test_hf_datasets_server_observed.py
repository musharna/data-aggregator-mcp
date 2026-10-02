"""What the datasets-server lookup sends and how it reads what comes back, pinned exactly.

Shapes from the live API (2026-10-02): see ``test_hf_datasets_server_answers.py``.
"""

import logging

import httpx
import pytest

from data_aggregator_mcp import _http, hf_datasets_server
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test_hf_datasets_server_answers import _C4

_DSS = "https://datasets-server.huggingface.co/parquet"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _entry(i: int, **over) -> dict:
    return {"config": "c", "split": "s", "url": f"https://h/c/s/{i:04d}.parquet", "size": i} | over


@pytest.mark.asyncio
async def test_the_request_is_exactly_what_datasets_server_is_sent():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_C4)

    async with _client(handler) as c:
        await hf_datasets_server.parquet_files(c, "allenai/c4")
    assert [(s.method, str(s.url), s.headers.get("accept")) for s in sent] == [
        ("GET", f"{_DSS}?dataset=allenai%2Fc4", "application/json")
    ]
    assert sent[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), hf_datasets_server.DEFAULT_TIMEOUT
    )


@pytest.mark.asyncio
async def test_an_outage_is_retried_once_and_named():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503)

    async with _client(handler) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] HF datasets-server exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await hf_datasets_server.parquet_files(c, "o/n")
    assert len(sent) == hf_datasets_server.MAX_RETRIES == 2


@pytest.mark.parametrize("status", [400, 403, 422])
@pytest.mark.asyncio
async def test_any_other_error_status_is_an_outage(status):
    async with _client(lambda r: httpx.Response(status, text="nope")) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=rf"^\[UpstreamUnavailableError\] HF datasets-server → HTTP {status}: nope$",
        ):
            await hf_datasets_server.parquet_files(c, "o/n")


@pytest.mark.parametrize("size", [None, 0, 7])
@pytest.mark.asyncio
async def test_a_file_maps_to_its_entry_and_size_may_be_null(size):
    body = {"parquet_files": [_entry(3, size=size)], "partial": False}
    async with _client(lambda r: httpx.Response(200, json=body)) as c:
        (f,) = await hf_datasets_server.parquet_files(c, "o/n")
    assert f.model_dump() == {
        "name": "c/s/0003.parquet",
        "size": size,
        "mime": None,
        "url": "https://h/c/s/0003.parquet",
        "checksum": None,
        "source": "hf-datasets-server",
    }


@pytest.mark.asyncio
async def test_a_file_without_a_size_is_read():
    entry = {k: v for k, v in _entry(3).items() if k != "size"}
    async with _client(lambda r: httpx.Response(200, json={"parquet_files": [entry]})) as c:
        (f,) = await hf_datasets_server.parquet_files(c, "o/n")
    assert (f.name, f.size) == ("c/s/0003.parquet", None)


@pytest.mark.parametrize(
    "n", [hf_datasets_server.MAX_DSS_FILES, hf_datasets_server.MAX_DSS_FILES + 1]
)
@pytest.mark.asyncio
async def test_the_file_list_is_capped_in_order_and_the_cap_is_logged(n, caplog):
    body = {"parquet_files": [_entry(i) for i in range(n)], "partial": False}
    async with _client(lambda r: httpx.Response(200, json=body)) as c:
        with caplog.at_level(logging.WARNING, logger=hf_datasets_server.logger.name):
            files = await hf_datasets_server.parquet_files(c, "o/n")
    cap = hf_datasets_server.MAX_DSS_FILES
    assert [f.name for f in files] == [f"c/s/{i:04d}.parquet" for i in range(cap)]
    expected = (
        [] if n == cap else [f"datasets-server: o/n exposes {n} parquet files; capping to {cap}"]
    )
    assert [m.getMessage() for m in caplog.records] == expected
