"""Each OpenML answer beside how the adapter must read it.

OpenML reports a query it cannot satisfy as HTTP ``412`` with
``{"error": {"code": ...}}``. Probed live 2026-10-01: an unknown dataset id
(``/data/99999999``, ``/data/100``, ``/data/00``) is code ``111`` "Unknown dataset",
``/data/0`` is code ``110`` "Please provide data_id", a name that matches nothing is
code ``372`` "No results". A real record and a real list always carry the objects
the adapter reads, so a 200 without them is a malformed answer, not "nothing".
"""

import httpx
import pytest

from data_aggregator_mcp import _http, openml
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_IRIS = {
    "data_set_description": {
        "id": "61",
        "name": "iris",
        "url": "https://api.openml.org/data/v1/download/61/iris.arff",
        "md5_checksum": "ad484452702105cbf3d30f8deaba39a9",
        "upload_date": "2014-04-06T23:23:39",
        "creator": ["R.A. Fisher"],
        "tag": "Botany",
    }
}
_ONE_HIT = {"data": {"dataset": [{"did": 61, "name": "iris"}]}}


def _error(code: str, message: str) -> httpx.Response:
    return httpx.Response(412, json={"error": {"code": code, "message": message}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(answers: dict[str, httpx.Response], seen: list[str]) -> httpx.AsyncClient:
    """Answer by the last path segment (the dataset id, or the list limit)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return answers[request.url.path.rsplit("/", 1)[-1]]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_an_unknown_dataset_is_not_found_not_an_outage():
    """Live: an id OpenML has no dataset for is ``412`` code ``111``. It was reported
    as ``UpstreamUnavailableError`` ("OpenML resolve → HTTP 412"), so asking for a
    dataset that does not exist read as the service being down."""
    seen: list[str] = []
    answers = {
        "99999999": _error("111", "Unknown dataset"),
        "7": _error("113", "Something else"),
        "61": httpx.Response(200, json=_IRIS),
    }
    async with _client(answers, seen) as c:
        with pytest.raises(NotFoundError, match="OpenML has no dataset 99999999"):
            await openml.resolve(c, "openml:99999999")
        # Any other 412 code is still a failure, not "no dataset".
        with pytest.raises(UpstreamUnavailableError, match="HTTP 412"):
            await openml.resolve(c, "openml:7")
        r = await openml.resolve(c, "openml:61")  # positive control
    assert r.title == "iris"


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_positive_integer_is_malformed_without_a_request():
    """OpenML dataset ids start at 1. ``openml:0`` passed the old ``[0-9]+`` check and
    went out as a request, answered ``412`` code ``110`` → reported as an outage."""
    seen: list[str] = []
    answers = {
        "0": _error("110", "Please provide data_id"),
        "00": _error("111", "Unknown dataset"),
        "61": httpx.Response(200, json=_IRIS),
    }
    async with _client(answers, seen) as c:
        for rid in ("openml:0", "openml:00"):
            with pytest.raises(NotFoundError, match="malformed OpenML id"):
                await openml.resolve(c, rid)
        assert seen == []
        r = await openml.resolve(c, "openml:61")  # positive control
    assert r.id == "openml:61" and seen == ["/api/v1/json/data/61"]


def _iris_as_sent(did: str) -> httpx.Response:
    """Live: OpenML builds ``parquet_url`` from the id as sent; ``dataset_0061.pq`` is
    a 404 while ``dataset_61.pq`` downloads (checked 2026-10-01)."""
    desc = {
        **_IRIS["data_set_description"],
        "parquet_url": f"https://data.openml.org/datasets/0000/0061/dataset_{did}.pq",
    }
    return httpx.Response(200, json={"data_set_description": desc})


@pytest.mark.asyncio
async def test_a_zero_padded_id_is_resolved_as_its_dataset():
    """``openml:0061`` went out as /data/0061: the record came back as a second id for
    iris (``openml:0061``) carrying a parquet URL that does not exist."""
    seen: list[str] = []
    answers = {"0061": _iris_as_sent("0061"), "61": _iris_as_sent("61")}
    async with _client(answers, seen) as c:
        padded = await openml.resolve(c, "openml:0061")
        plain = await openml.resolve(c, "openml:61")  # positive control
    assert seen == ["/api/v1/json/data/61"] * 2
    assert padded == plain
    assert padded.id == "openml:61"
    parquet = padded.files[-1].url
    assert parquet is not None and parquet.endswith("/dataset_61.pq")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data_set_description": None},
        {"data_set_description": []},
        {"data_set_description": "iris"},
        {"data_set_description": {"id": "61"}},
        {"data_set_description": {**_IRIS["data_set_description"], "upload_date": 2014}},
        {"data_set_description": {**_IRIS["data_set_description"], "url": ["a.arff"]}},
        {"data_set_description": {**_IRIS["data_set_description"], "description": 5}},
        {"data_set_description": {**_IRIS["data_set_description"], "tag": {"a": 1}}},
    ],
)
@pytest.mark.asyncio
async def test_a_record_without_its_description_is_a_malformed_answer(body):
    """A 200 lacking ``data_set_description`` (or carrying one of the wrong shape)
    was read as "OpenML has no dataset", or escaped as a bare ``AttributeError`` /
    ``TypeError`` / pydantic error. OpenML's real "no dataset" is the 412 above."""
    seen: list[str] = []
    answers = {"5": httpx.Response(200, json=body), "61": httpx.Response(200, json=_IRIS)}
    async with _client(answers, seen) as c:
        with pytest.raises(UpstreamUnavailableError, match="200 body.*no dataset description in"):
            await openml.resolve(c, "openml:5")
        r = await openml.resolve(c, "openml:61")  # positive control
    assert [f.name for f in r.files] == ["iris.arff"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": None},
        {"data": {}},
        {"data": {"dataset": None}},
        {"data": {"dataset": {"did": 61, "name": "iris"}}},
        {"data": {"dataset": [61]}},
        {"data": {"dataset": [{"name": "iris"}]}},
        {"data": {"dataset": [{"did": True, "name": "iris"}]}},
    ],
)
@pytest.mark.asyncio
async def test_a_list_without_its_datasets_is_a_malformed_answer(body):
    """A 200 lacking ``data.dataset`` was read as zero hits; entries that are not
    objects crashed, and an entry without a ``did`` became ``openml:None``. OpenML's
    real "no match" is the 412 code 372."""
    seen: list[str] = []
    async with _client({"5": httpx.Response(200, json=body)}, seen) as c:
        with pytest.raises(UpstreamUnavailableError, match="200 body.*no dataset list in"):
            await openml.search(c, "iris", size=5)
    async with _client({"5": httpx.Response(200, json=_ONE_HIT)}, seen) as c:
        total, recs = await openml.search(c, "iris", size=5)  # positive control
    assert total == 1 and [r.id for r in recs] == ["openml:61"]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(412, json=[1]),
        httpx.Response(412, json={"error": "No results"}),
        httpx.Response(412, json="372"),
        httpx.Response(412, json={"error": None}),
        httpx.Response(412, text="<html>Precondition Failed</html>"),
    ],
)
@pytest.mark.asyncio
async def test_a_412_without_an_error_object_is_a_failure_not_a_crash(answer):
    """Only ``{"error": {"code": "372"}}`` means "no match". A 412 whose body is not
    that object (a proxy's page, a bare string) must be reported as the failure it
    is; reading ``.get`` off a list or string escaped as a bare ``AttributeError``."""
    seen: list[str] = []
    async with _client({"5": answer}, seen) as c:
        with pytest.raises(UpstreamUnavailableError, match="HTTP 412"):
            await openml.search(c, "iris", size=5)
    async with _client({"5": _error("372", "No results")}, seen) as c:
        assert await openml.search(c, "iris", size=5) == (0, [])  # positive control
