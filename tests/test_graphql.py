"""A GraphQL answer off its contract is a named failure, never an empty answer (#88
burn-down of `openneuro`; its GraphQL call and `pdb`'s now share ``_http.graphql``).

GraphQL answers 200 whatever happened, and both modules read the body leniently after
checking ``errors``: a body without ``data`` was an empty OpenNeuro manifest or "no PDB
entry", a string where a list belongs raised a bare AttributeError, and a ``urls``
string became the download URL ``"h"``. Each case sits beside the real answer it must
not be confused with. Shapes are the live services' answers and their introspected
schemas on 2026-10-01: ``Snapshot.files: [DatasetFile]``, ``DatasetFile.filename:
String!``, ``urls: [String]``; RCSB ``entries: [CoreEntry]``, ``rcsb_id: String!``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from data_aggregator_mcp import _http, openneuro, pdb
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_URL = "https://gql.example.org/graphql"
_DOI = "10.18112/openneuro.ds000001.v1.0.0"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


class _Server:
    """Answers every request with ``body`` (or a pdb search hit) and records them."""

    def __init__(self, body: object) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "search.rcsb.org":
            return httpx.Response(
                200, json={"total_count": 1, "result_set": [{"identifier": "1BG2"}]}
            )
        return httpx.Response(200, content=json.dumps(self.body).encode())

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _unparseable(service: str, tries: int, cause: str) -> str:
    envelope = _http.UpstreamEnvelopeError(cause)
    return (
        f"[UpstreamUnavailableError] {service} returned an unparseable 200 body after {tries} "
        f"tries: {envelope!r}"
    )


# --- the shared helper ------------------------------------------------------------


async def test_graphql_posts_the_query_and_returns_data():
    srv = _Server({"data": {"a": 1}})
    async with srv.client() as c:
        assert await _http.graphql(c, _URL, "{a}", service="S") == {"a": 1}
        assert await _http.graphql(c, _URL, "{b}", service="S", variables={"v": 2}) == {"a": 1}
    plain, with_vars = srv.requests
    assert (plain.method, str(plain.url)) == ("POST", _URL)
    assert plain.headers["Content-Type"] == "application/json"
    assert plain.headers["Accept"] == "application/json"
    assert json.loads(plain.content) == {"query": "{a}"}
    assert json.loads(with_vars.content) == {"query": "{b}", "variables": {"v": 2}}


@pytest.mark.parametrize(
    ("errors", "messages"),
    [
        ([{"message": "first"}, "second"], "first; second"),
        ("boom", "boom"),  # not a list: quoted whole, not character by character
    ],
)
async def test_graphql_errors_are_quoted_and_not_retried(errors, messages):
    srv = _Server({"errors": errors, "data": None})
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _http.graphql(c, _URL, "{a}", service="S", max_retries=3)
    assert str(err.value) == f"[UpstreamUnavailableError] S answered GraphQL errors: {messages}"
    assert len(srv.requests) == 1  # the same query gets the same errors


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": []}, {"data": "x"}, {"errors": []}])
async def test_graphql_without_a_data_object_is_malformed(body):
    async with _Server({"data": {}}).client() as c:  # an empty data object is an answer
        assert await _http.graphql(c, _URL, "{a}", service="S") == {}
    srv = _Server(body)
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _http.graphql(c, _URL, "{a}", service="S", max_retries=2)
    assert str(err.value) == _unparseable("S", 2, f"no data object in {body!r:.200}")
    assert len(srv.requests) == 2


async def test_graphql_tries_a_malformed_answer_three_times_by_default():
    srv = _Server({})
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError, match="after 3 tries"):
            await _http.graphql(c, _URL, "{a}", service="S")
    assert len(srv.requests) == 3


async def test_graphql_check_sees_data_and_its_failure_is_retried():
    seen: list[object] = []

    def check(data: dict) -> None:
        seen.append(data)
        if "bad" in data:
            raise _http.UpstreamEnvelopeError("bad data")

    async with _Server({"data": {"good": 1}}).client() as c:
        assert await _http.graphql(c, _URL, "{a}", service="S", check=check) == {"good": 1}
    assert seen == [{"good": 1}]
    srv = _Server({"data": {"bad": 1}})
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _http.graphql(c, _URL, "{a}", service="S", max_retries=2, check=check)
    assert str(err.value) == _unparseable("S", 2, "bad data")
    assert len(srv.requests) == 2


# --- OpenNeuro --------------------------------------------------------------------

_FILE = {"filename": "README", "size": 1, "directory": False, "urls": ["https://x/README"]}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"snapshot": []},
        {"snapshot": {"files": "x"}},
        {"snapshot": {"files": [None]}},  # a null file would shorten the manifest
        {"snapshot": {"files": [{**_FILE, "filename": None}]}},
        {"snapshot": {"files": [{**_FILE, "filename": 5}]}},
        {"snapshot": {"files": [{**_FILE, "urls": "https://x/README"}]}},
        {"snapshot": {"files": [{**_FILE, "urls": [5]}]}},
    ],
)
async def test_an_openneuro_snapshot_off_contract_is_a_failure_not_a_manifest(data):
    async with _Server({"data": {"snapshot": {"files": [_FILE]}}}).client() as c:
        files = await openneuro.files(c, _DOI)
    assert [(f.name, f.url) for f in files] == [("README", "https://x/README")]
    # Schema-legal nulls: a snapshot that is null, or one with no file list.
    for legal in ({"snapshot": None}, {"snapshot": {"files": None}}):
        async with _Server({"data": legal}).client() as c:
            assert await openneuro.files(c, _DOI) == []
    async with _Server({"data": data}).client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await openneuro.files(c, _DOI)
    assert str(err.value) == _unparseable(
        "OpenNeuro snapshot ds000001@1.0.0",
        openneuro.MAX_RETRIES,
        f"no snapshot file list in {data!r:.200}",
    )


async def test_an_openneuro_answer_without_data_is_a_failure_not_an_empty_manifest():
    async with _Server({"data": None}).client() as c:
        with pytest.raises(UpstreamUnavailableError, match="no data object"):
            await openneuro.files(c, _DOI)


async def test_a_missing_openneuro_snapshot_names_the_snapshot():
    # OpenNeuro's live answer for a tag it does not have: errors AND a null snapshot.
    body = {
        "errors": [{"message": "Not Found", "path": ["snapshot"]}],
        "data": {"snapshot": None},
    }
    async with _Server(body).client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await openneuro.files(c, _DOI)
    assert str(err.value) == (
        "[UpstreamUnavailableError] OpenNeuro snapshot ds000001@1.0.0 answered GraphQL "
        "errors: Not Found"
    )


async def test_an_openneuro_file_without_urls_stays_in_the_manifest():
    """It was dropped, so the manifest came back shorter than the snapshot with no word
    of it; fetch already reports a url-less file as skipped."""
    listing = [
        {"filename": "a.tsv", "size": 3, "directory": False, "urls": None},
        {"filename": "b.tsv", "size": 4, "directory": False, "urls": []},
        _FILE,
        {"filename": "sub-01", "size": 0, "directory": True, "urls": []},
        {**_FILE, "filename": "sub-01/c.tsv"},
    ]
    async with _Server({"data": {"snapshot": {"files": listing}}}).client() as c:
        files = await openneuro.files(c, _DOI)
    assert [(f.name, f.size, f.url) for f in files] == [
        ("a.tsv", 3, None),
        ("b.tsv", 4, None),
        ("README", 1, "https://x/README"),
        ("sub-01/c.tsv", 1, "https://x/README"),
    ]


# --- RCSB PDB ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "data", [{}, {"entries": {"rcsb_id": "1BG2"}}, {"entries": ["1BG2"]}, {"entries": "1BG2"}]
)
async def test_a_pdb_entries_answer_off_contract_is_a_failure_not_no_entry(data):
    async with _Server({"data": {"entries": [{"rcsb_id": "1BG2"}]}}).client() as c:
        assert (await pdb.resolve(c, "pdb:1BG2")).id == "pdb:1BG2"
    async with _Server({"data": {"entries": None}}).client() as c:  # schema-legal null
        with pytest.raises(NotFoundError):
            await pdb.resolve(c, "pdb:1BG2")
    srv = _Server({"data": data})
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await pdb.resolve(c, "pdb:1BG2")
    assert str(err.value) == _unparseable(
        "RCSB PDB graphql", pdb.MAX_RETRIES, f"no entries list in {data!r:.200}"
    )
    assert len(srv.requests) == pdb.MAX_RETRIES


@pytest.mark.parametrize("body", [{}, {"data": []}])
async def test_a_pdb_answer_without_data_is_a_failure_not_zero_entries(body):
    async with _Server(body).client() as c:
        with pytest.raises(UpstreamUnavailableError, match="no data object"):
            await pdb.search(c, "kinesin")
