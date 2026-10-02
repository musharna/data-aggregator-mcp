"""What the datasets-server lookup sends and how it reads what comes back, pinned exactly.

Shapes from the live API (2026-10-02): see ``test_hf_datasets_server_answers.py``.
"""

import logging

import httpx
import pytest

from data_aggregator_mcp import _http, hf_datasets_server, huggingface
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from tests.test_hf_datasets_server_answers import _C4, _GATED, _LICHESS, _SCRIPT
from tests.test_huggingface_answers import _SROIE

_DSS = "https://datasets-server.huggingface.co/parquet"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_BRANCH = "https://huggingface.co/datasets/o/n/resolve/refs%2Fconvert%2Fparquet"


def _entry(i: int, **over) -> dict:
    return {"config": "c", "split": "s", "url": f"{_BRANCH}/c/s/{i:04d}.parquet", "size": i} | over


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


@pytest.mark.parametrize(("status", "body"), [(401, _GATED), (501, _SCRIPT), (404, {"error": "x"})])
@pytest.mark.asyncio
async def test_no_readable_view_is_not_found_not_an_outage(status, body):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.params["dataset"] == "o/ok":
            return httpx.Response(200, json=_C4)
        return httpx.Response(status, json=body)

    async with _client(handler) as c:
        with pytest.raises(NotFoundError) as exc:
            await hf_datasets_server.parquet_files(c, "o/gated")
        # Positive control: the same client reads a converted dataset.
        assert len(await hf_datasets_server.parquet_files(c, "o/ok")) == 2
    if status == 404:
        assert str(exc.value).startswith("[NotFoundError] HF datasets-server → HTTP 404: ")
    else:
        assert str(exc.value) == (
            "[NotFoundError] HF datasets-server has no readable converted view of 'o/gated'"
        )
    assert len(sent) == 2  # a permanent answer: not retried


@pytest.mark.parametrize("status", [400, 403, 422])
@pytest.mark.asyncio
async def test_any_other_error_status_is_an_outage(status):
    async with _client(lambda r: httpx.Response(status, text="nope")) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=rf"^\[UpstreamUnavailableError\] HF datasets-server → HTTP {status}: nope$",
        ):
            await hf_datasets_server.parquet_files(c, "o/n")


@pytest.mark.parametrize(
    "body",
    [
        {"partial": False},  # no file list at all
        {"parquet_files": None, "partial": False},
        {"parquet_files": {"0": _entry(0)}, "partial": False},
        {"parquet_files": [_entry(0), "x"]},
        {"parquet_files": [_entry(0), _entry(1, url=None)]},
        {"parquet_files": [_entry(0), _entry(1, url="")]},
        {"parquet_files": [_entry(0), _entry(1, url=7)]},
        {"parquet_files": [_entry(0), _entry(1, url="0001.parquet")]},  # no path to name it by
        # not on the conversion branch
        {"parquet_files": [_entry(0), _entry(1, url="https://h/c/s/0001.parquet")]},
        {"parquet_files": [_entry(0), _entry(1, url=f"{_BRANCH}/s/0001.parquet")]},
        {"parquet_files": [_entry(0), _entry(1, url=f"{_BRANCH}/c/s/x/0001.parquet")]},
        {"parquet_files": [_entry(0), _entry(1, url=f"{_BRANCH}/c//0001.parquet")]},
        {"parquet_files": [_entry(0), _entry(1, url=f"{_BRANCH}/c/s/")]},
        {"parquet_files": [_entry(0), _entry(1, url=f"{_BRANCH}//s/0001.parquet")]},
        {"parquet_files": [_entry(0), _entry(1, size="12")]},
        {"parquet_files": [_entry(0), _entry(1, size=True)]},
        {"parquet_files": [_entry(0), _entry(1, size=1.5)]},
        {"parquet_files": [_entry(0), {k: v for k, v in _entry(1).items() if k != "url"}]},
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_200_is_retried_then_an_outage_never_fewer_files(body):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.params["dataset"] == "o/ok":
            return httpx.Response(200, json=_C4)
        return httpx.Response(200, json=body)

    async with _client(handler) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] HF datasets-server returned an unparseable "
            r"200 body after 2 tries: UpstreamEnvelopeError\(\"no datasets-server parquet "
            r"list in \{",
        ):
            await hf_datasets_server.parquet_files(c, "o/bad")
        assert len(sent) == 2
        # Positive control: a well-formed answer is read.
        assert len(await hf_datasets_server.parquet_files(c, "o/ok")) == 2


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
        "url": f"{_BRANCH}/c/s/0003.parquet",
        "checksum": None,
        "source": "hf-datasets-server",
    }


@pytest.mark.asyncio
async def test_a_file_without_a_size_is_read():
    entry = {k: v for k, v in _entry(3).items() if k != "size"}
    async with _client(lambda r: httpx.Response(200, json={"parquet_files": [entry]})) as c:
        (f,) = await hf_datasets_server.parquet_files(c, "o/n")
    assert (f.name, f.size) == ("c/s/0003.parquet", None)


@pytest.mark.asyncio
async def test_the_name_is_the_files_path_on_the_conversion_branch():
    """A split converted only in part keeps its ``partial-`` directory in the name, so a
    query on it cannot pass for one over the whole split; the parts of a split too
    large for one directory each get their own names."""
    sent = {"allenai/c4": _C4, "Lichess/standard-chess-games": _LICHESS}
    async with _client(lambda r: httpx.Response(200, json=sent[r.url.params["dataset"]])) as c:
        c4 = await hf_datasets_server.parquet_files(c, "allenai/c4")
        lichess = await hf_datasets_server.parquet_files(c, "Lichess/standard-chess-games")
    assert [(f.name, f.url) for f in c4] == [
        ("af/partial-train/0000.parquet", _C4["parquet_files"][0]["url"]),
        ("am/train/0000.parquet", _C4["parquet_files"][1]["url"]),
    ]
    assert [(f.name, f.url) for f in lichess] == [
        ("default/train-part0/0000.parquet", _LICHESS["parquet_files"][0]["url"]),
        ("default/train-part1/0000.parquet", _LICHESS["parquet_files"][1]["url"]),
    ]


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


@pytest.mark.asyncio
async def test_a_renamed_dataset_is_looked_up_by_its_current_name():
    """The Hub redirects an old name (``imdb``) to the dataset; datasets-server answers
    the old name 404 ``RenamedDatasetError``, which read as "no converted view"."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "datasets-server.huggingface.co":
            asked.append(request.url.params["dataset"])
            return httpx.Response(200, json=_C4)
        assert request.url.path == "/api/datasets/imdb"
        return httpx.Response(200, json={**_SROIE, "id": "stanfordnlp/imdb"})

    async with _client(handler) as c:
        r = await huggingface.resolve(c, "hf:imdb")
    assert asked == ["stanfordnlp/imdb"]
    assert r.id == "hf:stanfordnlp/imdb"
    assert [f.name for f in r.files if f.source == "hf-datasets-server"] == [
        "af/partial-train/0000.parquet",
        "am/train/0000.parquet",
    ]
