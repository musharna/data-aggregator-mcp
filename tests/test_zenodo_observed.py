"""What the Zenodo adapter sends and how it reads what comes back, pinned exactly.

Shapes from the live API (2026-10-02): see ``test_zenodo_answers.py``. The
``/versions/latest`` HEAD answers 301 with an absolute ``Location`` of the latest
record's API url (``_CONCEPT_OLD`` 7421899 → 10396807, in ``test_zenodo.py``).
"""

import copy
import logging

import httpx
import pytest

from data_aggregator_mcp import _http, zenodo
from data_aggregator_mcp._cache import TTLCache
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from tests.test_zenodo_answers import _FULL, _hits

_OLD, _NEW = 7421899, 10396807


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(zenodo, "_SEARCH_CACHE", TTLCache(maxsize=256, ttl=600))


def _superseded(rid: int = _OLD) -> dict:
    rec = copy.deepcopy(_FULL)
    rec["id"] = rid
    rec["metadata"]["relations"] = {"version": [{"index": 0, "is_last": False}]}
    return rec


def _routed(sent: list[httpx.Request], *, record: dict, latest: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/versions/latest"):
            return latest
        if request.url.path == "/api/records":
            return httpx.Response(200, json=_hits(record))
        return httpx.Response(200, json=record)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _moved(location: str) -> httpx.Response:
    return httpx.Response(301, headers={"Location": location})


def _call(r: httpx.Request) -> tuple[str, str, str | None]:
    return r.method, str(r.url), r.headers.get("accept")


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_zenodo_is_sent():
    sent: list[httpx.Request] = []
    latest = _moved(f"https://zenodo.org/api/records/{_NEW}")
    async with _routed(sent, record=_superseded(), latest=latest) as c:
        await zenodo.search(c, "climate")
        zenodo._SEARCH_CACHE.clear()
        r = await zenodo.resolve(c, f"zenodo:{_OLD}")
    assert [_call(s) for s in sent] == [
        ("GET", "https://zenodo.org/api/records?q=climate&size=10", "application/json"),
        ("GET", f"https://zenodo.org/api/records/{_OLD}", "application/json"),
        ("HEAD", f"https://zenodo.org/api/records/{_OLD}/versions/latest", "*/*"),  # httpx's
    ]
    # Positive control: the 301 was read, not followed, and names the newer version.
    assert r.superseded_by == f"zenodo:{_NEW}" and len(sent) == 3


@pytest.mark.asyncio
async def test_a_record_from_search_still_learns_its_newer_version_on_resolve():
    sent: list[httpx.Request] = []
    latest = _moved(f"https://zenodo.org/api/records/{_NEW}")
    async with _routed(sent, record=_superseded(), latest=latest) as c:
        await zenodo.search(c, "climate")
        r = await zenodo.resolve(c, f"zenodo:{_OLD}")  # served from the search cache
    assert [s.method for s in sent] == ["GET", "HEAD"]
    assert str(sent[1].url) == f"https://zenodo.org/api/records/{_OLD}/versions/latest"
    assert r.superseded_by == f"zenodo:{_NEW}"


@pytest.mark.asyncio
async def test_a_relative_location_is_read_against_the_lookup_url():
    async with _routed([], record=_superseded(), latest=_moved(f"/api/records/{_NEW}")) as c:
        r = await zenodo.resolve(c, f"zenodo:{_OLD}")
    assert r.superseded_by == f"zenodo:{_NEW}" and r.errors == {}


@pytest.mark.asyncio
async def test_a_failed_newer_version_lookup_says_what_failed(caplog):
    sent: list[httpx.Request] = []
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.zenodo")
    async with _routed(sent, record=_superseded(), latest=httpx.Response(503)) as c:
        r = await zenodo.resolve(c, f"zenodo:{_OLD}")
    heads = [s for s in sent if s.method == "HEAD"]
    assert len(heads) == 2  # two tries, not _http's default three
    assert r.errors == {
        "superseded_by": "UpstreamUnavailableError: [UpstreamUnavailableError] Zenodo latest "
        "version exhausted 2 retries (last HTTP 503)"
    }
    [msg] = [rec.getMessage() for rec in caplog.records]
    assert msg.startswith(
        f"zenodo latest-version lookup failed for {_OLD}: UpstreamUnavailableError("
    )
    # Positive control: a Location without a record id is a failure too, and says so.
    async with _routed(
        [], record=_superseded(), latest=_moved("https://zenodo.org/records/x")
    ) as c:
        r = await zenodo.resolve(c, f"zenodo:{_OLD}")
    assert r.superseded_by is None and r.errors == {
        "superseded_by": "UpstreamUnavailableError: [UpstreamUnavailableError] Zenodo "
        f"latest-version lookup for {_OLD}: HTTP 301, Location 'https://zenodo.org/records/x' "
        "— no record id"
    }


@pytest.mark.asyncio
async def test_errors_name_the_zenodo_service_and_the_reason():
    async with _routed([], record={"id": 1}, latest=httpx.Response(404)) as c:
        with pytest.raises(
            UpstreamUnavailableError, match=r"\] Zenodo resolve .*no Zenodo record in"
        ):
            await zenodo.resolve(c, "zenodo:1")

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as c:
        with pytest.raises(
            UpstreamUnavailableError, match=r"\] Zenodo search .*no Zenodo record list in"
        ):
            await zenodo.search(c, "climate")
        with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] malformed Zenodo id '../x'$"):
            await zenodo.resolve(c, "../x")


def test_null_text_fields_read_as_empty_and_the_rest_is_kept():
    rec = copy.deepcopy(_FULL)
    rec["metadata"]["title"] = None
    rec["metadata"]["creators"][0]["name"] = None
    rec["files"][0]["key"] = None
    rec["metadata"]["resource_type"] = None
    zenodo._check_record(rec)
    r = zenodo._normalize(rec)
    assert r.title == "" and r.creators[0].name == "" and r.files[0].name == ""
    assert r.kind == "other"
    # Positive control: the live record keeps every one of them, and its description.
    full = zenodo._normalize(_FULL)
    assert (
        full.title == "CLARITY D7.8 Data Management Plan"
        and full.creators[0].name == "Dihé, Pascal"
    )
    assert full.files[0].name == "D7.8 Data Management Plan v1.0-final.pdf"
    assert full.description == "<p>This report is the first deliverable of Task 7.3"


def test_a_grant_named_only_by_title_keeps_it_and_a_wrong_typed_title_is_refused():
    rec = copy.deepcopy(_FULL)
    rec["metadata"]["grants"] = [{"title": "CLARITY", "funder": {"name": "European Commission"}}]
    zenodo._check_record(rec)
    assert [(f.funder, f.award) for f in zenodo._normalize(rec).funding] == [
        ("European Commission", "CLARITY")
    ]
    rec["metadata"]["grants"][0]["title"] = 7
    with pytest.raises(_http.UpstreamEnvelopeError):
        zenodo._check_record(rec)


def test_a_wrong_typed_resource_type_is_refused_not_read_as_other():
    # ``_normalize`` reads ``str(type)``, so 7 would otherwise pass as kind "other".
    rec = copy.deepcopy(_FULL)
    rec["metadata"]["resource_type"] = {"type": "software"}
    zenodo._check_record(rec)
    assert zenodo._normalize(rec).kind == "software"  # positive control
    rec["metadata"]["resource_type"]["type"] = 7
    with pytest.raises(_http.UpstreamEnvelopeError, match="no Zenodo record in"):
        zenodo._check_record(rec)
