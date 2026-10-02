"""What the OpenML adapter sends and what it builds, value by value (#88 mutant
burn-down). Record fixtures are the live ``/data/61`` answer of 2026-10-01, trimmed
to the fields ``resolve`` reads."""

import httpx
import pytest

from data_aggregator_mcp import _http, openml
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Link

_ARFF = "https://openml.org/data/v1/download/61/iris.arff"
_PARQUET = "https://data.openml.org/datasets/0000/0061/dataset_61.pq"
_IRIS = {
    "id": "61",
    "name": "iris",
    "version": "1",
    "description": "**Author**: R.A. Fisher",
    "upload_date": "2014-04-06T23:23:39",
    "licence": "Public",
    "url": _ARFF,
    "parquet_url": _PARQUET,
    "md5_checksum": "ad484452702105cbf3d30f8deaba39a9",
    "creator": "R.A. Fisher",
    "tag": ["Botany", "Ecology"],
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _resolve(desc: dict, rid: str = "openml:61") -> DataResource:
    seen: list[httpx.Request] = []
    answer = httpx.Response(200, json={"data_set_description": desc})
    async with _recording(answer, seen) as c:
        return await openml.resolve(c, rid)


@pytest.mark.asyncio
async def test_resolve_requests_the_record_as_json():
    seen: list[httpx.Request] = []
    answer = httpx.Response(200, json={"data_set_description": _IRIS})
    async with _recording(answer, seen) as c:
        await openml.resolve(c, "openml: 61 ")  # surrounding space is trimmed
    [req] = seen
    assert req.method == "GET"
    assert str(req.url) == "https://www.openml.org/api/v1/json/data/61"
    assert req.headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_resolve_builds_every_field_of_the_record():
    assert await _resolve(_IRIS) == DataResource(
        id="openml:61",
        source="openml",
        kind="dataset",
        title="iris",
        description="**Author**: R.A. Fisher",
        creators=[Creator(name="R.A. Fisher")],
        year=2014,
        license="Public",
        access="open",
        subjects=["Botany", "Ecology"],
        last_updated="2014-04-06T23:23:39",
        files=[
            FileEntry(
                name="iris.arff",
                url=_ARFF,
                mime="text/plain",
                checksum="md5:ad484452702105cbf3d30f8deaba39a9",
                source="openml",
            ),
            FileEntry(
                name="dataset_61.pq",
                url=_PARQUET,
                mime="application/parquet",
                source="openml-parquet",
            ),
        ],
        links=[Link(rel="landing_page", target_id="https://www.openml.org/d/61")],
    )


@pytest.mark.asyncio
async def test_resolve_leaves_out_what_the_record_does_not_carry():
    """Live: dataset 1 has ``creator`` null; dataset 2 a list of names; 200 has no tags."""
    bare = await _resolve({"id": "61", "name": "iris", "url": _ARFF, "creator": ["A", "", "B"]})
    assert bare.creators == [Creator(name="A"), Creator(name="B")]
    assert [f.checksum for f in bare.files] == [None]  # no md5 → no checksum
    assert (bare.year, bare.last_updated, bare.description, bare.license) == (None,) * 4
    assert bare.subjects == []
    empty = await _resolve({"id": "61", "name": "iris", "creator": "", "md5_checksum": ""})
    assert empty.creators == [] and empty.files == []
    undated = await _resolve({"id": "61", "name": "iris", "upload_date": "unknown"})
    assert undated.year is None and undated.last_updated == "unknown"
    full = await _resolve(_IRIS)  # positive control
    assert full.year == 2014 and [c.name for c in full.creators] == ["R.A. Fisher"]


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (_PARQUET, "dataset_61.pq"),
        ("https://h/a/b/x.pq", "x.pq"),
        ("https://h/a/b/x.parquet", "x.parquet"),
        ("https://h/a/b/download", "dataset_61.pq"),
        ("https://h/a/b/x.PQ", "dataset_61.pq"),
        ("x.pq", "x.pq"),
    ],
)
def test_parquet_file_name(url, name):
    assert openml._parquet_name(url, "61") == name


@pytest.mark.asyncio
async def test_resolve_names_the_arff_by_its_last_path_segment():
    r = await _resolve({"id": "61", "name": "iris", "url": "https://h/a/b/iris.arff"})
    assert [f.name for f in r.files] == ["iris.arff"]


@pytest.mark.asyncio
async def test_search_requests_one_escaped_name_segment_capped_at_the_page_limit():
    seen: list[httpx.Request] = []
    body = {"data": {"dataset": [{"did": 61, "name": "iris"}, {"did": 969, "name": "iris"}]}}
    async with _recording(httpx.Response(200, json=body), seen) as c:
        total, recs = await openml.search(c, "a/b c", size=99)
    [req] = seen
    assert req.method == "GET"
    assert req.url.raw_path == b"/api/v1/json/data/list/data_name/a%2Fb%20c/limit/50"
    assert req.headers["Accept"] == "application/json"
    assert total == 2
    assert recs == [
        DataResource(id="openml:61", source="openml", kind="dataset", title="iris"),
        DataResource(id="openml:969", source="openml", kind="dataset", title="iris"),
    ]


@pytest.mark.asyncio
async def test_search_default_page_size_is_ten():
    seen: list[httpx.Request] = []
    async with _recording(httpx.Response(200, json={"data": {"dataset": []}}), seen) as c:
        assert await openml.search(c, "iris") == (0, [])
    assert seen[0].url.path.endswith("/limit/10")


@pytest.mark.asyncio
async def test_a_body_less_answer_is_zero_hits_for_search_and_no_dataset_for_resolve():
    seen: list[httpx.Request] = []
    async with _recording(httpx.Response(204), seen) as c:
        assert await openml.search(c, "iris", size=5) == (0, [])
        with pytest.raises(NotFoundError, match="^\\[NotFoundError\\] OpenML has no dataset 61$"):
            await openml.resolve(c, "openml:61")


@pytest.mark.parametrize(
    ("call", "service"),
    [
        (lambda c: openml.search(c, "iris", size=5), "OpenML search"),
        (lambda c: openml.resolve(c, "openml:61"), "OpenML resolve"),
    ],
)
@pytest.mark.asyncio
async def test_an_outage_names_the_service_and_its_two_tries(call, service):
    seen: list[httpx.Request] = []
    async with _recording(httpx.Response(503), seen) as c:
        with pytest.raises(UpstreamUnavailableError) as exc:
            await call(c)
    assert str(exc.value) == (
        f"[UpstreamUnavailableError] {service} exhausted 2 retries (last HTTP 503)"
    )
    assert len(seen) == 2
