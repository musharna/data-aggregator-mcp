"""How the E-utilities helpers read what NCBI answers, including the answers that are failures.

NCBI reports most failures inside an HTTP 200. Live shapes (2026-10-02, no API key):

- esearch, invalid db: ``{"header": …, "esearchresult": {"ERROR": "Invalid db name specified: notadb"}}``
- esummary, invalid db or empty id list: ``{"header": …, "esummaryresult": ["Invalid db name …"]}``
  (no ``result`` at all)
- elink, invalid db: ``{"header": …, "linksets": [], "ERROR": "Invalid db name specified: notadb"}``
- elink, a PMID with no edge: ``{"linksets": [{"dbfrom": "pubmed", "ids": ["99"]}]}`` (no ``linksetdbs``)

Read without a check, each failure became an empty success: no hits, "no such record",
no data links.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _eutils, _http
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_HEADER = {"type": "esearch", "version": "0.3"}
ESEARCH_OK = {
    "header": _HEADER,
    "esearchresult": {
        "count": "5713666",
        "retmax": "2",
        "retstart": "0",
        "idlist": ["42823380", "42823363"],
        "translationset": [],
    },
}
ESUMMARY_OK = {
    "header": {"type": "esummary", "version": "0.3"},
    "result": {
        "uids": ["1", "99999999999"],
        "1": {"uid": "1", "title": "Formate assay in body fluids."},
        "99999999999": {"uid": "99999999999", "error": "cannot get document summary"},
    },
}
ELINK_OK = {
    "header": {"type": "elink", "version": "0.3"},
    "linksets": [
        {
            "dbfrom": "pubmed",
            "ids": ["42162664"],
            "linksetdbs": [{"dbto": "gds", "linkname": "pubmed_gds", "links": ["200319641"]}],
        }
    ],
}
ELINK_NO_EDGE = {"header": ELINK_OK["header"], "linksets": [{"dbfrom": "pubmed", "ids": ["99"]}]}


@pytest.fixture(autouse=True)
def _offline(request, monkeypatch):
    """No backoff and no NCBI rate limit for the mocked tests; the live ones keep both."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    if request.node.name.startswith("test_live_"):
        return

    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(_http._ratelimit, "acquire", _ns)


def _client(body: object, sent: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _esearch(body: object) -> tuple[int, list[str]]:
    async with _client(body, []) as client:
        return await _eutils.esearch(client, "pubmed", "cancer", retmax=2)


async def _esummary(body: object) -> list[dict]:
    async with _client(body, []) as client:
        return await _eutils.esummary(client, "pubmed", ["1", "99999999999"])


async def _elink(body: object) -> list[str]:
    async with _client(body, []) as client:
        return await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664"])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"header": _HEADER}, id="no-esearchresult"),
        pytest.param({"esearchresult": None}, id="null-esearchresult"),
        pytest.param({"esearchresult": {}}, id="empty-esearchresult"),
        pytest.param({"esearchresult": {"count": "3"}}, id="no-idlist"),
        pytest.param({"esearchresult": {"idlist": ["1"]}}, id="no-count"),
        pytest.param({"esearchresult": {"count": "abc", "idlist": []}}, id="count-not-a-number"),
        pytest.param({"esearchresult": {"count": "-1", "idlist": []}}, id="count-negative"),
        pytest.param({"esearchresult": {"count": 2, "idlist": ["1", "2"]}}, id="count-not-string"),
        pytest.param({"esearchresult": {"count": "1", "idlist": "1"}}, id="idlist-string"),
        pytest.param({"esearchresult": {"count": "1", "idlist": [1]}}, id="id-not-string"),
    ],
)
async def test_esearch_a_malformed_answer_is_an_error_not_no_hits(body: object) -> None:
    assert await _esearch(ESEARCH_OK) == (5713666, ["42823380", "42823363"])  # positive control
    assert await _esearch({"esearchresult": {"count": "0", "idlist": []}}) == (0, [])
    sent: list[httpx.Request] = []
    async with _client(body, sent) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] NCBI esearch \(pubmed\) returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(\"no NCBI esearch result in ",
        ):
            await _eutils.esearch(client, "pubmed", "cancer", retmax=2)
    assert len(sent) == 3  # retried like any malformed body


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"header": {}, "esummaryresult": ["Invalid db name specified: notadb"]},
            id="esummaryresult-envelope",
        ),
        pytest.param({"result": None}, id="null-result"),
        pytest.param({"result": []}, id="result-list"),
        pytest.param({"result": {}}, id="no-uids"),
        pytest.param({"result": {"uids": "1", "1": {"uid": "1"}}}, id="uids-string"),
        pytest.param({"result": {"uids": [1], "1": {"uid": "1"}}}, id="uid-not-string"),
        pytest.param({"result": {"uids": ["1"]}}, id="listed-uid-without-summary"),
        pytest.param({"result": {"uids": ["1"], "1": "x"}}, id="summary-not-object"),
    ],
)
async def test_esummary_a_malformed_answer_is_an_error_not_missing_records(body: object) -> None:
    docs = await _esummary(ESUMMARY_OK)  # positive control: the per-uid error is dropped
    assert docs == [{"uid": "1", "title": "Formate assay in body fluids."}]
    sent: list[httpx.Request] = []
    async with _client(body, sent) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] NCBI esummary \(pubmed\) returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(\"no NCBI esummary result in ",
        ):
            await _eutils.esummary(client, "pubmed", ["1", "99999999999"])
    assert len(sent) == 3


async def test_elink_error_envelope_is_an_error_not_no_links() -> None:
    assert await _elink(ELINK_OK) == ["200319641"]  # positive control
    assert await _elink(ELINK_NO_EDGE) == []  # a PMID with no edge is still no links
    body = {"header": {}, "linksets": [], "ERROR": "Invalid db name specified: notadb"}
    sent: list[httpx.Request] = []
    async with _client(body, sent) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] NCBI elink \(pubmed->gds\) returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\('NCBI elink ERROR: Invalid db name specified: notadb'\)$",
        ):
            await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664"])
    assert len(sent) == 3


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"header": {}}, id="no-linksets"),
        pytest.param({"linksets": None}, id="null-linksets"),
        pytest.param({"linksets": {"dbfrom": "pubmed"}}, id="linksets-object"),
        pytest.param({"linksets": ["x"]}, id="linkset-not-object"),
        pytest.param({"linksets": [{"linksetdbs": None}]}, id="null-linksetdbs"),
        pytest.param({"linksets": [{"linksetdbs": ["x"]}]}, id="linksetdb-not-object"),
        pytest.param({"linksets": [{"linksetdbs": [{"links": "123"}]}]}, id="links-string"),
        pytest.param({"linksets": [{"linksetdbs": [{"links": None}]}]}, id="null-links"),
        pytest.param({"linksets": [{"linksetdbs": [{"links": [123]}]}]}, id="link-not-string"),
    ],
)
async def test_elink_a_malformed_answer_is_an_error_not_no_links(body: object) -> None:
    assert await _elink(ELINK_OK) == ["200319641"]  # positive control
    sent: list[httpx.Request] = []
    async with _client(body, sent) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] NCBI elink \(pubmed->gds\) returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(\"no NCBI elink linksets in ",
        ):
            await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664"])
    assert len(sent) == 3


async def test_elink_reads_a_linkset_whose_linksetdb_has_no_links_as_no_links() -> None:
    body = {"linksets": [{"linksetdbs": [{"dbto": "gds"}, {"links": ["7"]}]}]}
    assert await _elink(body) == ["7"]


# --- real execution: NCBI's own failure answers (no API key; 3 req/s) ---


@live_only
async def test_live_esummary_failure_is_an_error_not_missing_records() -> None:
    async with httpx.AsyncClient() as client:
        docs = await _eutils.esummary(client, "pubmed", ["1"])  # positive control
        assert [d["uid"] for d in docs] == ["1"]
        with pytest.raises(UpstreamUnavailableError, match="Invalid db name specified: notadb"):
            await _eutils.esummary(client, "notadb", ["1"])


@live_only
async def test_live_elink_failure_is_an_error_not_no_links() -> None:
    async with httpx.AsyncClient() as client:
        uids = await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664"])
        assert "200319641" in uids  # positive control
        assert await _eutils.elink(client, dbfrom="pubmed", db="sra", ids=["99"]) == []
        with pytest.raises(UpstreamUnavailableError, match="Invalid db name specified: notadb"):
            await _eutils.elink(client, dbfrom="pubmed", db="notadb", ids=["42162664"])


@live_only
async def test_live_esearch_answers_pass_the_check() -> None:
    async with httpx.AsyncClient() as client:
        count, ids = await _eutils.esearch(client, "pubmed", "cancer", retmax=2)
        assert count > 1_000_000 and len(ids) == 2
        assert await _eutils.esearch(client, "pubmed", "zzqqxxyyzz", retmax=2) == (0, [])


# --- field walk: every field, every wrong JSON type, refused or read cleanly ---

_WRONG = [None, True, 0, 1.5, "s", [], {}, ["s"], [0]]


def _paths(node: object, path: tuple = ()) -> list[tuple]:
    out = [path] if path else []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _paths(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _paths(v, (*path, i))
    return out


def _with(body: object, path: tuple, value: object) -> object:
    import copy

    out = copy.deepcopy(body)
    node: Any = out
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return out


_READERS = [
    pytest.param(_esearch, ESEARCH_OK, id="esearch"),
    pytest.param(_esummary, ESUMMARY_OK, id="esummary"),
    pytest.param(_elink, ELINK_OK, id="elink"),
]


@pytest.mark.parametrize(("read", "full"), _READERS)
async def test_no_wrong_typed_field_escapes_as_a_bare_error(read, full: dict) -> None:
    await read(full)  # positive control: the full live-shaped answer reads cleanly
    escapes = []
    for path in _paths(full):
        for value in _WRONG:
            try:
                await read(_with(full, path, value))
            except UpstreamUnavailableError:
                pass
            except Exception as exc:  # noqa: BLE001 - every escape is collected and reported
                escapes.append((path, value, repr(exc)))
    assert escapes == []
