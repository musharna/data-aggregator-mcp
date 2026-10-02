"""Each DataONE Solr answer beside how the adapter must read it.

Probed live 2026-10-01 (cn.dataone.org/cn/v2/query/solr/):
- Every update leaves the old version indexed with ``obsoletedBy`` set: 565,416 of
  1,639,871 METADATA objects, and 108,568 of 110,671 hits for "salmon" (41 of the first
  50). DataONE's own search UI (MetacatUI ``Search.js``) excludes ``obsoletedBy:*``.
- A query with no hits is ``200 {"responseHeader": ..., "response": {"numFound": 0,
  "start": 0, "docs": []}}``. A real answer always carries ``response.numFound`` and
  ``response.docs``.
- 2,000 random docs: every field the adapter reads came back at the type below or
  absent (author 8%, origin 14%, fileName 23% absent; size always an int).
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, dataone
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _answer(*docs: dict, found: int | None = None) -> dict:
    n = len(docs) if found is None else found
    return {
        "responseHeader": {"status": 0},
        "response": {"numFound": n, "start": 0, "docs": list(docs)},
    }


# Verbatim from the live index (fl as the adapter asks for it).
_CORAL = {
    "identifier": "doi:10.5063/AA/knb-csun-usvi.10700",
    "formatId": "eml://ecoinformatics.org/eml-2.1.0",
    "dateUploaded": "2013-07-19T22:43:24.469Z",
    "dateModified": "2013-11-14T20:33:32.233Z",
    "title": "Virgin Islands National Park: Coral Reef: Decadal-scale changes in community "
    "structure from 1987 to 2011",
    "datePublished": "2013-01-01T00:00:00Z",
    "author": "Peter Edmunds",
    "origin": ["California State University Northridge", "Peter Edmunds"],
    "resourceMap": ["resourceMap_autogen.2013071915432447802.1"],
}
_WIND = {
    "identifier": "ark:/13030/m5dn93vh/1/Wind__statistics.xlsx",
    "size": 18251,
    "checksum": "4a54db2d145f7e15d070910975b3544c6dc56bb5",
    "checksumAlgorithm": "SHA-1",
}
_MN_URL = "https://mn.example/object/wind"


def _resolver(meta: object, data: object) -> httpx.AsyncClient:
    """Solr answers ``data`` to the package query and ``meta`` to any other; the CN
    resolve answers 303 to a Member-Node url, as it does live."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/resolve/" in request.url.path:
            return httpx.Response(303, headers={"location": _MN_URL})
        if request.url.params["q"].startswith("resourceMap:"):
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=meta)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_search_asks_for_the_latest_version_only():
    """Search asked for every indexed version, so 98% of "salmon" hits and the total were
    superseded copies of a few datasets."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json=_answer(_CORAL, found=2103))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dataone.search(c, "salmon", size=5)
    assert seen == ["(salmon) AND formatType:METADATA AND -obsoletedBy:*"]
    # Positive control: the answer still reads whole.
    assert total == 2103 and [r.id for r in recs] == ["dataone:doi:10.5063/AA/knb-csun-usvi.10700"]


_MALFORMED = [
    {},
    {"response": None},
    {"response": {"docs": []}},
    {"response": {"numFound": "1", "docs": []}},
    {"response": {"numFound": True, "docs": []}},
    {"response": {"numFound": 1}},
    {"response": {"numFound": 1, "docs": _CORAL}},
    {"response": {"numFound": 1, "docs": [{"title": "no identifier"}]}},
    {"response": {"numFound": 1, "docs": [{"identifier": "", "title": "empty identifier"}]}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED)
async def test_a_malformed_answer_is_upstream_trouble_not_an_empty_one(body):
    """A 200 without ``response.numFound``/``docs`` read as zero hits, so resolve
    reported a real object as not found, and search reported no results."""
    async with _resolver(body, body) as c:
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await dataone.search(c, "soil")
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await dataone.resolve(c, "dataone:x")
    # Positive control: the live no-hit answer is still zero hits / not found.
    async with _resolver(_answer(), _answer()) as c:
        assert await dataone.search(c, "soil") == (0, [])
        with pytest.raises(NotFoundError):
            await dataone.resolve(c, "dataone:x")


@pytest.mark.asyncio
async def test_live_shaped_docs_read_whole():
    async with _resolver(_answer(_CORAL), _answer(_WIND)) as c:
        r = await dataone.resolve(c, "dataone:doi:10.5063/AA/knb-csun-usvi.10700")
    assert r.doi == "10.5063/AA/knb-csun-usvi.10700" and r.year == 2013
    assert r.creators == [
        Creator(name="California State University Northridge"),
        Creator(name="Peter Edmunds"),
    ]
    [f] = r.files
    # No fileName: the PID names the file; SHA-1 becomes the hashlib name sha1.
    assert f.name == _WIND["identifier"] and f.url == _MN_URL and f.size == 18251
    assert f.checksum == "sha1:4a54db2d145f7e15d070910975b3544c6dc56bb5"


_FULL = {**_CORAL, **_WIND, "identifier": "doi:10.5063/AA/x", "fileName": "wind.xlsx"}
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(path, value):
    doc = copy.deepcopy(_FULL)
    *parents, last = path
    node = doc
    for p in parents:
        node = node[p]
    node[last] = value
    return doc


@pytest.mark.asyncio
async def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Walk every field of a doc carrying all the fields read (metadata and data) and
    every wrong JSON type: each must be refused by the answer check (terminal
    UpstreamUnavailableError) or read cleanly, so a field read without the check
    fails here."""
    # Positive control: the full doc passes the check and resolves whole.
    dataone._check_solr(_answer(_FULL))
    async with _resolver(_answer(_FULL), _answer(_FULL)) as c:
        full = await dataone.resolve(c, "dataone:doi:10.5063/AA/x")
    assert full.title == _CORAL["title"] and len(full.creators) == 2
    assert [(f.name, f.size, f.checksum) for f in full.files] == [
        ("wind.xlsx", 18251, "sha1:4a54db2d145f7e15d070910975b3544c6dc56bb5")
    ]
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            doc = _with(path, value)
            async with _resolver(_answer(doc), _answer(doc)) as c:
                try:
                    await dataone.search(c, "x")
                    await dataone.resolve(c, "dataone:x")
                except UpstreamUnavailableError:
                    continue
                except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                    escaped.append((path, value, type(exc).__name__))
    assert escaped == []
