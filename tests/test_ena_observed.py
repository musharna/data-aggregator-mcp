"""What the ENA client sends and how it reports a failed call, pinned exactly.

Shapes from the live API (2026-10-02): see ``test_ena_answers.py``.
"""

from __future__ import annotations

from urllib.parse import parse_qsl

import httpx
import pytest

from data_aggregator_mcp import _http, ena
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test_ena_answers import _SRX079566

_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,"
    "scientific_name,fastq_ftp,fastq_bytes,fastq_md5"
)


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(sent: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_filereport_sends_exactly_the_documented_get():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=_SRX079566)) as c:
        files = await ena.filereport(c, "SRX079566")
    assert len(files) == 4
    assert [(r.method, r.url.scheme, r.url.host, r.url.path) for r in sent] == [
        ("GET", "https", "www.ebi.ac.uk", "/ena/portal/api/filereport")
    ]
    assert parse_qsl(sent[0].url.query.decode(), keep_blank_values=True) == [
        ("accession", "SRX079566"),
        ("result", "read_run"),
        ("fields", _FIELDS),
        ("format", "json"),
    ]


@pytest.mark.asyncio
async def test_a_crafted_accession_stays_a_query_value():
    """The accession is a query parameter, so it cannot reach another endpoint or add a
    parameter; ENA answers a malformed one with 400 (see the live test)."""
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=[])) as c:
        assert await ena.filereport(c, "../../evil?injected=1&format=xml#x") == []
        assert await ena.filereport(c, "SRX079566") == []  # control: same request shape
    for r in sent:
        assert r.url.path == "/ena/portal/api/filereport"
        assert [k for k, _ in parse_qsl(r.url.query.decode())] == [
            "accession",
            "result",
            "fields",
            "format",
        ]
    assert [dict(parse_qsl(r.url.query.decode()))["accession"] for r in sent] == [
        "../../evil?injected=1&format=xml#x",
        "SRX079566",
    ]


@pytest.mark.asyncio
async def test_a_failing_upstream_is_named_and_tried_three_times():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] ENA filereport exhausted 3 retries "
            r"\(last HTTP 503\)$",
        ):
            await ena.filereport(c, "SRX079566")
    assert len(sent) == 3
    sent.clear()
    async with _recording(sent, httpx.Response(200, json={"message": "x"})) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] ENA filereport returned an unparseable 200 "
            r"body after 3 tries: UnexpectedShapeError\('expected list JSON, got dict",
        ):
            await ena.filereport(c, "SRX079566")
    assert len(sent) == 3
    # ENA's answer to a malformed accession is a 400, reported as such, not retried.
    sent.clear()
    async with _recording(sent, httpx.Response(400, json={"message": "not valid"})) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] ENA filereport → HTTP 400: ",
        ):
            await ena.filereport(c, "foo")
    assert len(sent) == 1
    # Positive control: the same client reads a real answer.
    async with _recording(sent, httpx.Response(200, json=_SRX079566)) as c:
        assert len(await ena.filereport(c, "SRX079566")) == 4
