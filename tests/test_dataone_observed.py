"""What the DataONE adapter sends and reads, pinned against live answers.

Probed live 2026-10-01: the Solr query and CN resolve endpoints, the answer shapes
quoted in ``test_dataone_answers.py``, and a CN resolve answer (below) taken verbatim.
"""

import logging

import httpx
import pytest

from data_aggregator_mcp import _http, dataone
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _answer(*docs: dict, found: int | None = None) -> dict:
    n = len(docs) if found is None else found
    return {"response": {"numFound": n, "start": 0, "docs": list(docs)}}


_CORAL = {
    "identifier": "doi:10.5063/AA/knb-csun-usvi.10700",
    "title": "Virgin Islands National Park: Coral Reef",
    "dateUploaded": "2013-07-19T22:43:24.469Z",
    "datePublished": "2013-01-01T00:00:00Z",
    "origin": ["Peter Edmunds"],
    "resourceMap": ["resourceMap_autogen.2013071915432447802.1"],
}
_WIND = {
    "identifier": "ark:/13030/m5dn93vh/1/Wind__statistics.xlsx",
    "size": 18251,
    "checksum": "4a54db2d145f7e15d070910975b3544c6dc56bb5",
    "checksumAlgorithm": "SHA-1",
}
_MN_URL = "https://arcticdata.io/metacat/d1/mn/v2/object/urn:uuid:875cb8ca"

# Verbatim CN /cn/v2/resolve/ body (303, urn:uuid:875cb8ca-d83e-4822-af08-11b44a17e6d6):
# the children carry no namespace, and <identifier> has text before <url>.
_OBJLOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns2:objectLocationList xmlns:ns2="http://ns.dataone.org/service/types/v1">
    <identifier>urn:uuid:875cb8ca-d83e-4822-af08-11b44a17e6d6</identifier>
    <objectLocation>
        <nodeIdentifier>urn:node:ARCTIC</nodeIdentifier>
        <baseURL>https://arcticdata.io/metacat/d1/mn</baseURL>
        <version>v1</version>
        <version>v2</version>
        <url>https://arcticdata.io/metacat/d1/mn/v2/object/urn:uuid:875cb8ca-d83e-4822-af08-11b44a17e6d6</url>
    </objectLocation>
</ns2:objectLocationList>"""


def _recording(sent: list[httpx.Request], meta: dict, data: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "/resolve/" in request.url.path:
            return httpx.Response(303, headers={"location": _MN_URL})
        if request.url.params["q"].startswith("resourceMap:"):
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=meta)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _solr_call(request: httpx.Request) -> tuple[str, str, dict, str | None]:
    url = str(request.url.copy_with(query=None))
    return request.method, url, dict(request.url.params), request.headers.get("accept")


_SEARCH_FL = (
    "identifier,title,author,origin,formatId,dateUploaded,datePublished,dateModified,resourceMap"
)
_SOLR = "https://cn.dataone.org/cn/v2/query/solr/"


@pytest.mark.asyncio
async def test_search_sends_exactly_the_solr_query():
    sent: list[httpx.Request] = []
    async with _recording(sent, _answer(_CORAL, found=2103), _answer()) as c:
        total, recs = await dataone.search(c, "salmon")
        await dataone.search(c, "salmon", size=200, offset=7)
    q = "(salmon) AND formatType:METADATA AND -obsoletedBy:*"
    page = {"q": q, "fl": _SEARCH_FL, "wt": "json"}
    assert [_solr_call(r) for r in sent] == [
        ("GET", _SOLR, {**page, "rows": "10", "start": "0"}, "application/json"),
        ("GET", _SOLR, {**page, "rows": "50", "start": "7"}, "application/json"),  # capped
    ]
    # Positive control: the answer reads whole.
    assert total == 2103 and [r.id for r in recs] == [f"dataone:{_CORAL['identifier']}"]


@pytest.mark.asyncio
async def test_resolve_sends_exactly_the_record_package_and_locator_requests():
    sent: list[httpx.Request] = []
    async with _recording(sent, _answer(_CORAL), _answer(_WIND)) as c:
        r = await dataone.resolve(c, "dataone:doi:10.5063/AA/knb-csun-usvi.10700")
    record, package, locate = sent
    assert _solr_call(record) == (
        "GET",
        _SOLR,
        {
            "q": 'identifier:"doi\\:10.5063\\/AA\\/knb\\-csun\\-usvi.10700"',
            "fl": "identifier,title,author,origin,dateUploaded,datePublished,dateModified,resourceMap",
            "rows": "1",
            "start": "0",
            "wt": "json",
        },
        "application/json",
    )
    assert _solr_call(package) == (
        "GET",
        _SOLR,
        {
            "q": 'resourceMap:"resourceMap_autogen.2013071915432447802.1" AND formatType:DATA',
            "fl": "identifier,fileName,size,checksum,checksumAlgorithm",
            "rows": "50",
            "start": "0",
            "wt": "json",
        },
        "application/json",
    )
    # The PID is one path segment: every "/" and ":" escaped.
    assert locate.method == "GET" and locate.url.raw_path == (
        b"/cn/v2/resolve/ark%3A%2F13030%2Fm5dn93vh%2F1%2FWind__statistics.xlsx"
    )
    # Positive control: the record and its file read whole.
    assert r.id == f"dataone:{_CORAL['identifier']}"
    assert [(f.name, f.url, f.source) for f in r.files] == [
        (_WIND["identifier"], _MN_URL, "dataone")
    ]


@pytest.mark.asyncio
async def test_errors_name_the_dataone_service_and_object():
    async with _recording([], {"response": {}}, _answer()) as c:
        with pytest.raises(
            UpstreamUnavailableError, match=r"DataONE search .*no DataONE result list"
        ):
            await dataone.search(c, "soil")

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="ServiceFailure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
        with pytest.raises(UpstreamUnavailableError, match="DataONE resolve"):
            await dataone._object_url(c, "urn:uuid:x")
    async with _recording([], _answer(), _answer()) as c:
        with pytest.raises(NotFoundError, match="DataONE has no object 'nope'"):
            await dataone.resolve(c, "dataone:nope")
        # Positive control: a record that exists resolves.
    async with _recording([], _answer(_CORAL), _answer()) as c:
        assert (await dataone.resolve(c, "dataone:x")).title == _CORAL["title"]


def test_year_is_the_publication_year_over_the_upload_year():
    # Verbatim (HydroShare): uploaded 2019, published 2008.
    globec = {
        "identifier": "sha256:8cbb35c7a3ad7de3f1697d5ff71a7163564e9fb2806dd37220c20aef039e6c58",
        "dateUploaded": "2019-02-01T16:19:00Z",
        "dateModified": "2022-04-15T19:34:22Z",
        "title": "Inventory of U.S. GLOBEC Southern Ocean data",
        "datePublished": "2008-05-07T00:00:00Z",
    }
    assert dataone._normalize(globec).year == 2008
    unpublished = {k: v for k, v in globec.items() if k != "datePublished"}
    assert dataone._normalize(unpublished).year == 2019
    untitled = {k: v for k, v in globec.items() if k != "title"}
    assert dataone._normalize(untitled).title == ""


def test_the_locator_url_is_read_from_the_live_object_location_list():
    url = "https://arcticdata.io/metacat/d1/mn/v2/object/urn:uuid:875cb8ca-d83e-4822-af08-11b44a17e6d6"
    assert dataone._first_url(_OBJLOC) == url
    namespaced = '<l xmlns="http://ns.dataone.org/service/types/v1"><identifier>x</identifier><url> u </url></l>'
    assert dataone._first_url(namespaced) == "u"
    assert dataone._first_url("<l><identifier>x</identifier></l>") is None


@pytest.mark.asyncio
async def test_a_package_over_the_cap_reads_exactly_the_cap_and_says_so(caplog):
    starts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/resolve/" in request.url.path:
            return httpx.Response(303, headers={"location": _MN_URL})
        q = request.url.params["q"]
        if not q.startswith("resourceMap:"):
            return httpx.Response(200, json=_answer({**_CORAL, "resourceMap": ["rm"]}))
        start = int(request.url.params["start"])
        starts.append(str(start))
        page = [{"identifier": f"obj{i}"} for i in range(start, min(start + 50, 1200))]
        return httpx.Response(200, json=_answer(*page, found=1200))

    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.dataone")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await dataone.resolve(c, "dataone:x")
    assert len(r.files) == 1000 and r.files[-1].name == "obj999"
    assert starts == [str(n) for n in range(0, 1000, 50)]  # 20 pages, not a 21st
    assert [rec.getMessage() for rec in caplog.records] == [
        'DataONE package resourceMap:"rm" AND formatType:DATA holds 1200 data objects; '
        "attaching the first 1000"
    ]
