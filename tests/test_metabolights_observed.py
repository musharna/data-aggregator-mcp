"""What the MetaboLights file lister sends, and its errors, pinned exactly.

The listing shapes are in ``test_metabolights_answers.py``.
"""

import httpx
import pytest

from data_aggregator_mcp import _http, metabolights
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry
from tests.test_metabolights_answers import _NAMES, _PAGE

_DIR = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/MTBLS1/"
_HASHES = _DIR + "HASHES/metadata_sha256.json"
_SHA = "cd615abd1532ae3ba70efb35681710a6de985aef1c03a836ec2c3c824cffde7a"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(sent: list[httpx.Request], listing: httpx.Response, hashes: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return hashes if str(request.url) == _HASHES else listing

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _calls(sent: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(r.method, str(r.url)) for r in sent]


@pytest.mark.asyncio
async def test_files_sends_the_listing_then_the_hash_list_request():
    sent: list[httpx.Request] = []
    hashes = httpx.Response(200, json={_NAMES[0]: _SHA})
    async with _client(sent, httpx.Response(200, text=_PAGE), hashes) as c:
        got = await metabolights.files(c, "MTBLS1")
    assert _calls(sent) == [("GET", _DIR), ("GET", _HASHES)]
    assert got[0] == FileEntry(
        name=_NAMES[0],
        url=_DIR + _NAMES[0],
        checksum=f"sha256:{_SHA}",
        source="metabolights",
    )
    assert [f.model_dump() for f in got[1:]] == [
        {
            "name": n,
            "size": None,
            "mime": None,
            "url": _DIR + n,
            "checksum": None,
            "source": "metabolights",
        }
        for n in _NAMES[1:]
    ]


@pytest.mark.asyncio
async def test_an_unknown_study_is_not_found_and_nothing_more_is_asked():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(404, text="gone"), httpx.Response(200, json={})) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] MetaboLights files → HTTP 404: gone$"
        ):
            await metabolights.files(c, "MTBLS1")
    assert _calls(sent) == [("GET", _DIR)]


@pytest.mark.asyncio
async def test_a_failing_listing_is_tried_twice_then_reported():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(503), httpx.Response(200, json={})) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] MetaboLights files exhausted 2 retries \(last HTTP 503\)$",
        ):
            await metabolights.files(c, "MTBLS1")
    assert _calls(sent) == [("GET", _DIR), ("GET", _DIR)]


@pytest.mark.asyncio
async def test_a_failing_hash_list_is_tried_twice_then_reported():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, text=_PAGE), httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] MetaboLights hashes exhausted 2 retries \(last HTTP 503\)$",
        ):
            await metabolights.files(c, "MTBLS1")
    assert _calls(sent) == [("GET", _DIR), ("GET", _HASHES), ("GET", _HASHES)]


@pytest.mark.asyncio
async def test_a_hash_entry_that_is_not_a_digest_string_leaves_its_file_unverified():
    """41 of 41 live entries (9 studies, 2026-10-02) are 64-hex strings; anything else
    in the slot is no checksum, never one made from it (`sha256:7`, `sha256:`)."""
    body = {_NAMES[0]: _SHA, _NAMES[1]: 7, _NAMES[2]: "", _NAMES[3]: None}
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, text=_PAGE), httpx.Response(200, json=body)) as c:
        got = await metabolights.files(c, "MTBLS1")
    assert [f.checksum for f in got] == [f"sha256:{_SHA}", None, None, None]
