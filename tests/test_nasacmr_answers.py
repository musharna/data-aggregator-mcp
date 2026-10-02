"""Each NASA CMR answer beside how the adapter must read it.

Probed live 2026-10-02 (cmr.earthdata.nasa.gov/search/collections.umm_json):
- A keyword with no hits is ``200 {"hits": 0, "took": 8, "items": []}``; an unknown but
  well-formed concept id (``C0-NONE``, ``G123-X``, ``C2586786218-pocloud``) is the same.
- A concept id of any other shape is ``400 {"errors": ["Concept-id [foo] is not
  valid."]}`` (``foo``, ``c2586786218-POCLOUD``, a trailing space, two ids joined by a
  comma), and ``../../evil`` is a CloudFront ``403 Request blocked``.
- 1,750 collections (250 at each of offsets 0-44,000 of all 45,100): every concept id is
  ``C<digits>-<provider>``, and every field the adapter reads came back at the type
  below or null (``UseConstraints`` null for 789, ``DataDates`` for 1,064,
  ``DOI.DOI`` for 1,146: those carry ``MissingReason`` instead).
"""

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, nasacmr
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, Link


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


# Trimmed from the live record of C2586786218-POCLOUD (2026-10-02); UseConstraints is
# the live shape of the collections that carry a LicenseURL and a FreeAndOpenData flag.
_FULL = {
    "meta": {"concept-id": "C2586786218-POCLOUD", "revision-date": "2026-09-28T14:24:52.636Z"},
    "umm": {
        "EntryTitle": "GHRSST Level 4 OSTIA Global Historical Reprocessed Foundation Sea "
        "Surface Temperature Analysis produced by the UK Meteorological Office",
        "Abstract": "The Operational Sea Surface Temperature and Sea Ice Analysis Reprocessed",
        "DOI": {"DOI": "10.5067/GHOST-4RM02", "Authority": "https://doi.org/"},
        "DataCenters": [{"ShortName": "NASA/JPL/PODAAC"}, {"ShortName": "UK/MOD/MET"}],
        "ScienceKeywords": [
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": "SEA ICE"},
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": "OCEAN TEMPERATURE"},
        ],
        "UseConstraints": {
            "Description": "Free to use.",
            "FreeAndOpenData": True,
            "LicenseURL": {"Linkage": "https://creativecommons.org/licenses/by/4.0/"},
            "LicenseText": "See the licence.",
        },
        "RelatedUrls": [
            {
                "URL": "https://podaac.jpl.nasa.gov/CitingPODAAC",
                "Type": "VIEW RELATED INFORMATION",
            },
            {
                "URL": "https://search.earthdata.nasa.gov/search/granules?p=C2586786218-POCLOUD",
                "Type": "GET DATA",
                "Subtype": "Earthdata Search",
            },
        ],
        "DataDates": [
            {"Type": "CREATE", "Date": "2013-02-14T01:44:31.886Z"},
            {"Type": "UPDATE", "Date": "2017-04-28T05:01:46.000Z"},
        ],
    },
}
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]
_UNPARSEABLE = (
    r"^\[UpstreamUnavailableError\] NASA CMR {} returned an unparseable 200 body after 3 "
    r"tries: UpstreamEnvelopeError\([\"']no NASA CMR collection list in \{{"
)


def _page(*items: object, hits: object = None) -> dict:
    return {"hits": len(items) if hits is None else hits, "took": 5, "items": list(items)}


def _with(path, value, base=None):
    item = copy.deepcopy(_FULL if base is None else base)
    *parents, last = path
    node = item
    for p in parents:
        node = node[p]
    node[last] = value
    return item


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _serve(body: object, sent: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_BROKEN = {
    "no hits or items": {},
    "items but no hit count": {"took": 5, "items": []},
    "a hit count but no items": {"hits": 3, "took": 5},
    "a string hit count": _page(hits="1"),
    "a boolean hit count": _page(hits=True),
    "items not a list": {"hits": 1, "items": {"meta": {}}},
    "an item that is not an object": _page("C2586786218-POCLOUD"),
    "an item without meta": _page({"umm": _FULL["umm"]}),
    "an item without umm": _page({"meta": _FULL["meta"]}),
    "an item without a concept id": _page(_with(("meta", "concept-id"), None)),
    "a granule concept id": _page(_with(("meta", "concept-id"), "G1-POCLOUD")),
    "a DOI that is a string": _page(_with(("umm", "DOI"), "10.5067/GHOST-4RM02")),
    "a data center name that is a list": _page(_with(("umm", "DataCenters", 0, "ShortName"), [])),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", list(_BROKEN.values()), ids=list(_BROKEN))
async def test_a_malformed_answer_is_an_outage_not_no_hits(body):
    """A 200 without a hit count and a collection list was read as zero hits (or a
    record id of ``nasacmr:``), and a wrong-typed field escaped as a bare
    AttributeError / TypeError / pydantic error. Each is now retried, then reported."""
    # Positive controls: the real no-hit answer and the real record are read.
    async with _serve(_page(hits=0)) as c:
        assert await nasacmr.search(c, "zzqqxxnotaword") == (0, [])
    async with _serve(_page(_FULL, hits=4111)) as c:
        total, recs = await nasacmr.search(c, "sst")
    assert total == 4111 and [r.id for r in recs] == ["nasacmr:C2586786218-POCLOUD"]

    sent: list[httpx.Request] = []
    async with _serve(body, sent) as c:
        with pytest.raises(UpstreamUnavailableError, match=_UNPARSEABLE.format("search")):
            await nasacmr.search(c, "sst")
        with pytest.raises(UpstreamUnavailableError, match=_UNPARSEABLE.format("resolve")):
            await nasacmr.resolve(c, "nasacmr:C2586786218-POCLOUD")
    assert len(sent) == 6  # each call retried to the budget, not read once and accepted


def _variant(keyword: dict, related_url: dict) -> dict:
    """``_FULL`` with its first keyword and related URL replaced: the walk only reaches a
    fallback leaf (Topic, Category) or a GET DATA URL when the record makes the reader
    read it."""
    item = copy.deepcopy(_FULL)
    item["umm"]["ScienceKeywords"][0] = keyword
    item["umm"]["RelatedUrls"][0] = related_url
    return item


_GET_DATA = {"URL": "https://search.earthdata.nasa.gov/x", "Type": "GET DATA"}
_WALKED = {
    "full": (_FULL, ["SEA ICE", "OCEAN TEMPERATURE"]),
    "topic fallback": (
        _variant({"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": None}, _GET_DATA),
        ["OCEANS", "OCEAN TEMPERATURE"],
    ),
    "category fallback": (
        _variant({"Category": "EARTH SCIENCE", "Topic": None, "Term": None}, _GET_DATA),
        ["EARTH SCIENCE", "OCEAN TEMPERATURE"],
    ),
}


@pytest.mark.parametrize(("base", "subjects"), list(_WALKED.values()), ids=list(_WALKED))
def test_no_wrong_typed_field_escapes_as_a_bare_error(base, subjects):
    """Walk every field of a record with every wrong JSON type: each must be refused by
    the check or normalized cleanly, so a field the reader starts using without the check
    fails here. The variants make the reader reach the keyword fallbacks and a GET DATA
    URL, which the full record never reads."""
    # Positive control: the record passes the check and reads whole.
    nasacmr._check_page(_page(base))
    full = nasacmr._normalize(base)
    assert full.creators == [Creator(name="NASA/JPL/PODAAC"), Creator(name="UK/MOD/MET")]
    assert full.license == "CC-BY-4.0" and full.year == 2013 and full.doi == "10.5067/GHOST-4RM02"
    assert full.subjects == subjects and len(full.links) == 1
    escaped = []
    for path in _paths(base):
        if not path:
            continue
        for value in _WRONG:
            item = _with(path, value, base)
            try:
                nasacmr._check_page(_page(item))
            except _http.UpstreamEnvelopeError:
                continue
            try:
                nasacmr._normalize(item)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


def test_the_tolerated_schema_violations_still_pass_the_check():
    """The 2026 audit (L1/L2) chose to skip null list entries and a non-string
    UseConstraints leaf rather than fail the search leg; the check keeps that."""
    item = _with(("umm", "DataCenters"), [None, {"ShortName": "NASA/X"}])
    item["umm"]["UseConstraints"] = {"Description": {"nested": "object not a string"}}
    item["umm"]["RelatedUrls"] = [None, 7, {"Type": "GET DATA", "URL": "https://earthdata/x"}]
    nasacmr._check_page(_page(item))
    r = nasacmr._normalize(item)
    assert r.creators == [Creator(name="NASA/X")] and r.license is None
    assert r.links == [Link(rel="data_access", target_id="https://earthdata/x")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local",
    [
        "foo",
        "c2586786218-POCLOUD",
        "C2586786218-POCLOUD ",
        "C2586786218-POCLOUD,C1588876556-EUMETSAT",
        "G2586786218-POCLOUD",
        "../../evil?injected=1",
        "",
    ],
)
async def test_a_malformed_id_is_not_found_before_the_network(local):
    """CMR answers a concept id of the wrong shape with HTTP 400, which resolve reported
    as an outage (``NASA CMR resolve → HTTP 400``). Now it is refused before any request."""
    sent: list[httpx.Request] = []
    async with _serve(_page(_FULL), sent) as c:
        r = await nasacmr.resolve(c, "nasacmr:C2586786218-POCLOUD")  # positive control
        assert r.id == "nasacmr:C2586786218-POCLOUD" and len(sent) == 1
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] malformed NASA CMR collection id 'nasacmr:"
        ) as info:
            await nasacmr.resolve(c, f"nasacmr:{local}")
    assert info.value.args == (f"malformed NASA CMR collection id {'nasacmr:' + local!r}",)
    assert len(sent) == 1


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_a_malformed_id_is_not_found_and_cmr_would_have_refused_it():
    async with httpx.AsyncClient(timeout=60) as c:
        raw = await c.get(nasacmr.SEARCH, params={"concept_id": "foo"})
        assert raw.status_code == 400 and raw.json() == {
            "errors": ["Concept-id [foo] is not valid."]
        }
        with pytest.raises(
            NotFoundError,
            match=r"^\[NotFoundError\] malformed NASA CMR collection id 'nasacmr:foo'\Z",
        ):
            await nasacmr.resolve(c, "nasacmr:foo")
        # Positive control: a well-formed unknown id is asked for, and is not found.
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] NASA CMR has no collection 'C0-NONE'\Z"
        ):
            await nasacmr.resolve(c, "nasacmr:C0-NONE")


@_live_only
@pytest.mark.asyncio
async def test_live_search_at_the_largest_tool_size_passes_the_check():
    """50 live records (the tool's largest page) each pass ``_check_page``, and every id
    resolves back to itself."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await nasacmr.search(c, "sea surface temperature", size=50)
        assert total > 50 and len(recs) == 50
        r = await nasacmr.resolve(c, recs[-1].id)
    assert r.id == recs[-1].id and r.title == recs[-1].title
