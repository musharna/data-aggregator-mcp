"""What the PRIDE adapter sends and how it reads what comes back, pinned exactly.

Shapes are the live captures in ``test_pride_answers.py``.
"""

import httpx
import pytest

from data_aggregator_mcp import _http, pride
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry
from tests.test_pride_answers import ROW, ROW_FILE

_PROJECT = "https://www.ebi.ac.uk/pride/ws/archive/v3/projects"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(sent: list[httpx.Request], count, pages) -> httpx.AsyncClient:
    """``count`` answers /files/count; ``pages`` answers each /files request in turn.
    Either may be an ``httpx.Response`` or a list of them (served in order)."""

    def _next(answer):
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/files/count"):
            return _next(count)
        return _next(pages)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call(r: httpx.Request) -> tuple[str, str, str | None]:
    return r.method, str(r.url), r.headers.get("accept")


def _row(name, *values, size=7):
    return {
        "fileName": name,
        "fileSizeBytes": size,
        "publicFileLocations": [{"name": "loc", "value": v} for v in values],
    }


@pytest.mark.asyncio
async def test_files_sends_exactly_these_requests():
    rows = [_row(f"r{i}.raw", f"ftp://ftp.pride.ebi.ac.uk/p/r{i}.raw") for i in range(150)]
    sent: list[httpx.Request] = []
    pages = [httpx.Response(200, json=rows[:100]), httpx.Response(200, json=rows[100:])]
    async with _client(sent, httpx.Response(200, json=150), pages) as c:
        assert len(await pride.files(c, "PXD000150")) == 150
    assert [_call(r) for r in sent] == [
        ("GET", f"{_PROJECT}/PXD000150/files/count", "application/json"),
        ("GET", f"{_PROJECT}/PXD000150/files?pageSize=100&page=0", "application/json"),
        ("GET", f"{_PROJECT}/PXD000150/files?pageSize=100&page=1", "application/json"),
    ]


@pytest.mark.asyncio
async def test_a_failing_count_is_tried_twice_and_named():
    """Positive control: one 503 and then the count is retried into a full listing."""
    sent: list[httpx.Request] = []
    flaky = [httpx.Response(503), httpx.Response(200, json=1)]
    async with _client(sent, flaky, httpx.Response(200, json=[ROW])) as c:
        assert await pride.files(c, "PXD000001") == [ROW_FILE]
    assert len(sent) == 3
    sent.clear()
    async with _client(sent, httpx.Response(503), httpx.Response(200, json=[ROW])) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] PRIDE file count exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await pride.files(c, "PXD000001")
    assert [_call(r) for r in sent] == [
        ("GET", f"{_PROJECT}/PXD000001/files/count", "application/json")
    ] * 2


@pytest.mark.asyncio
async def test_a_failing_page_is_tried_twice_and_named():
    """Positive control: one 503 and then the page is retried into a full listing."""
    sent: list[httpx.Request] = []
    flaky = [httpx.Response(503), httpx.Response(200, json=[ROW])]
    async with _client(sent, httpx.Response(200, json=1), flaky) as c:
        assert await pride.files(c, "PXD000001") == [ROW_FILE]
    assert len(sent) == 3
    sent.clear()
    async with _client(sent, httpx.Response(200, json=1), httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] PRIDE files exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await pride.files(c, "PXD000001")
    page = ("GET", f"{_PROJECT}/PXD000001/files?pageSize=100&page=0", "application/json")
    assert [_call(r) for r in sent] == [
        ("GET", f"{_PROJECT}/PXD000001/files/count", "application/json"),
        page,
        page,
    ]


@pytest.mark.asyncio
async def test_each_row_is_read_by_its_first_public_location():
    """The first FTP or HTTPS location wins, in the order listed: an FTP path is served
    over HTTPS from the same host, an HTTPS url is kept as it is, and Aspera or a null
    location is passed over. A row with no public location (none listed, or null) is
    left out without stopping the rows after it."""
    ftp = "ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/a.raw"
    https = "https://example.org/pride/b.raw"
    aspera = "prd_ascp@fasp.ebi.ac.uk:pride/data/archive/2012/03/PXD000001/a.raw"
    rows = [
        _row("none.raw", aspera),
        {**_row("null-list.raw"), "publicFileLocations": None},
        _row("a.raw", aspera, None, ftp, https),
        _row("b.raw", https, ftp),
        {k: v for k, v in _row("c.raw", ftp).items() if k != "fileSizeBytes"},
    ]
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=5), httpx.Response(200, json=rows)) as c:
        got = await pride.files(c, "PXD000001")
    served = "https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/a.raw"
    assert got == [
        FileEntry(name="a.raw", url=served, size=7, source="pride"),
        FileEntry(name="b.raw", url=https, size=7, source="pride"),
        FileEntry(name="c.raw", url=served, size=None, source="pride"),
    ]
