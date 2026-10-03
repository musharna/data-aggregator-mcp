"""What the E-utilities helpers send and how they report failure, pinned exactly.

Answer shapes are the live ones in ``test__eutils_answers.py``.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from data_aggregator_mcp import _eutils, _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test__eutils_answers import ELINK_OK, ESEARCH_OK, ESUMMARY_OK

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(_http._ratelimit, "acquire", _ns)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)


def _client(sent: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _only(sent: list[httpx.Request], path: str) -> dict[str, str]:
    """The one request sent: a GET of ``path`` with no repeated parameter."""
    (req,) = sent
    assert req.method == "GET"
    assert f"{req.url.scheme}://{req.url.host}{req.url.path}" == f"{_BASE}/{path}"
    params = req.url.params
    assert len(params.multi_items()) == len(params)
    return dict(params)


@pytest.mark.parametrize("key", [None, "k3y"])
async def test_esearch_request(monkeypatch, key: str | None) -> None:
    if key:
        monkeypatch.setenv("NCBI_API_KEY", key)
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=ESEARCH_OK)) as client:
        got = await _eutils.esearch(client, "pubmed", "cancer", retmax=2, retstart=40)
    assert got == (5713666, ["42823380", "42823363"])
    want = {"db": "pubmed", "term": "cancer", "retmax": "2", "retstart": "40", "retmode": "json"}
    assert _only(sent, "esearch.fcgi") == want | ({"api_key": key} if key else {})


@pytest.mark.parametrize("key", [None, "k3y"])
async def test_esummary_request(monkeypatch, key: str | None) -> None:
    if key:
        monkeypatch.setenv("NCBI_API_KEY", key)
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=ESUMMARY_OK)) as client:
        docs = await _eutils.esummary(client, "pubmed", ["1", "99999999999"])
    assert [d["uid"] for d in docs] == ["1"]
    want = {"db": "pubmed", "id": "1,99999999999", "version": "2.0", "retmode": "json"}
    assert _only(sent, "esummary.fcgi") == want | ({"api_key": key} if key else {})


@pytest.mark.parametrize("key", [None, "k3y"])
async def test_elink_request(monkeypatch, key: str | None) -> None:
    if key:
        monkeypatch.setenv("NCBI_API_KEY", key)
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=ELINK_OK)) as client:
        uids = await _eutils.elink(client, dbfrom="pubmed", db="gds", ids=["42162664", "7"])
    assert uids == ["200319641"]
    want = {"dbfrom": "pubmed", "db": "gds", "id": "42162664,7", "retmode": "json"}
    assert _only(sent, "elink.fcgi") == want | ({"api_key": key} if key else {})


@pytest.mark.parametrize("key", [None, "k3y"])
async def test_efetch_request(monkeypatch, key: str | None) -> None:
    if key:
        monkeypatch.setenv("NCBI_API_KEY", key)
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, text="<TaxaSet/>")) as client:
        assert await _eutils.efetch(client, "taxonomy", ["3701", "3702"]) == "<TaxaSet/>"
    want = {"db": "taxonomy", "id": "3701,3702", "retmode": "xml"}
    assert _only(sent, "efetch.fcgi") == want | ({"api_key": key} if key else {})


async def test_efetch_text_mode_returns_the_body_unvalidated() -> None:
    """``retmode="text"`` is not XML: the body is returned as served, on the first try.
    In XML mode the same body is malformed and retried (the control)."""
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, text="LOCUS <unbalanced")) as client:
        body = await _eutils.efetch(client, "nuccore", ["1"], retmode="text")
        assert body == "LOCUS <unbalanced"
        assert _only(sent, "efetch.fcgi") == {"db": "nuccore", "id": "1", "retmode": "text"}
        with pytest.raises(UpstreamUnavailableError, match=r"unparseable 200 body after 3 tries"):
            await _eutils.efetch(client, "nuccore", ["1"])
    assert len(sent) == 4


_CALLS = [
    pytest.param(
        lambda c: _eutils.esearch(c, "gds", "rice", retmax=1), "NCBI esearch (gds)", id="esearch"
    ),
    pytest.param(lambda c: _eutils.esummary(c, "sra", ["1"]), "NCBI esummary (sra)", id="esummary"),
    pytest.param(
        lambda c: _eutils.elink(c, dbfrom="bioproject", db="sra", ids=["1"]),
        "NCBI elink (bioproject->sra)",
        id="elink",
    ),
    pytest.param(lambda c: _eutils.efetch(c, "pubmed", ["1"]), "NCBI efetch (pubmed)", id="efetch"),
]


@pytest.mark.parametrize(("call", "service"), _CALLS)
async def test_each_call_names_its_service_and_retries_three_times(call, service: str) -> None:
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(503)) as client:
        with pytest.raises(UpstreamUnavailableError) as err:
            await call(client)
    assert str(err.value) == (
        f"[UpstreamUnavailableError] {service} exhausted 3 retries (last HTTP 503)"
    )
    assert len(sent) == 3


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(
            {"esearchresult": {"ERROR": "Search Backend failed: <internal>"}},
            "NCBI esearch ERROR: Search Backend failed: <internal>",
            id="ERROR",
        ),
        pytest.param(
            {"error": "API rate limit exceeded", "count": "11", "limit": "10"},
            "NCBI esearch error: API rate limit exceeded",
            id="error",
        ),
    ],
)
async def test_esearch_error_envelope_message(body: dict, message: str) -> None:
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=body)) as client:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _eutils.esearch(client, "gds", "rice", retmax=1)
    assert str(err.value) == (
        "[UpstreamUnavailableError] NCBI esearch (gds) returned an unparseable 200 body "
        f"after 3 tries: UpstreamEnvelopeError({message!r})"
    )


async def test_esearch_an_empty_ERROR_is_not_a_failure() -> None:
    body = {"esearchresult": {"count": "0", "idlist": [], "ERROR": ""}}
    async with _client([], httpx.Response(200, json=body)) as client:
        assert await _eutils.esearch(client, "gds", "rice", retmax=1) == (0, [])


async def test_esummary_error_envelope_message() -> None:
    body = {"error": "Invalid uid abc at position= 0", "result": {"uids": []}}  # live shape
    async with _client([], httpx.Response(200, json=body)) as client:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _eutils.esummary(client, "pubmed", ["abc"])
    assert str(err.value) == (
        "[UpstreamUnavailableError] NCBI esummary (pubmed) returned an unparseable 200 body "
        "after 3 tries: UpstreamEnvelopeError('NCBI esummary error: Invalid uid abc at position= 0')"
    )


async def test_esummary_drops_each_uid_without_a_record_and_logs_it(caplog) -> None:
    body = {
        "result": {
            "uids": ["1", "8", "2"],
            "1": {"uid": "1"},
            "8": {"uid": "8", "error": "cannot get document summary"},
            "2": {"uid": "2", "error": ""},
        }
    }
    caplog.set_level(logging.INFO, logger="data_aggregator_mcp._eutils")
    async with _client([], httpx.Response(200, json=body)) as client:
        docs = await _eutils.esummary(client, "pubmed", ["1", "8", "2"])
    assert docs == [{"uid": "1"}, {"uid": "2", "error": ""}]
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            "data_aggregator_mcp._eutils",
            logging.INFO,
            "NCBI esummary (pubmed): uid 8 has no record: cannot get document summary",
        )
    ]


async def test_elink_unions_linksets_in_first_seen_order() -> None:
    body = {
        "linksets": [
            {"linksetdbs": [{"links": ["3", "1"]}, {"links": ["1", "2"]}]},
            {"dbfrom": "pubmed"},
            {"linksetdbs": [{"links": ["2", "4"]}]},
        ]
    }
    async with _client([], httpx.Response(200, json=body)) as client:
        assert await _eutils.elink(client, dbfrom="pubmed", db="sra", ids=["9"]) == [
            "3",
            "1",
            "2",
            "4",
        ]


@pytest.mark.parametrize(
    "call",
    [
        lambda c: _eutils.esummary(c, "pubmed", []),
        lambda c: _eutils.elink(c, dbfrom="pubmed", db="gds", ids=[]),
        lambda c: _eutils.efetch(c, "pubmed", []),
    ],
    ids=["esummary", "elink", "efetch"],
)
async def test_no_ids_sends_no_request(call) -> None:
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=ESUMMARY_OK)) as client:
        assert not await call(client)
    assert sent == []
