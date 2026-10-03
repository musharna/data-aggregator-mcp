"""Each GBIF answer beside how the adapter must read it.

Probed live 2026-10-02 (api.gbif.org/v1, anonymous):
- A dataset key that is not a UUID is ``400 text/plain "Invalid UUID string: notauuid"``,
  which the adapter reported as an outage; an unknown UUID is ``404 "Entity not found for
  uri: /"``.
- A deleted dataset is still ``200`` with its full record and a ``deleted`` timestamp
  (25,205 of them in ``/v1/dataset/deleted``); its archive URL answers 404. The adapter
  returned it as a live dataset with a fetchable archive.
- A query with no hits is ``200 {"offset": 0, "limit": 10, "endOfRecords": true, "count": 0,
  "results": [], "facets": []}``. A real answer always carries ``results`` and an int
  ``count``.
- 900 search hits (9 pages of 100 at random offsets of the 124,561-dataset index) and
  40 dataset records drawn from them: every field the adapter reads came back at the
  type below or absent or null, so the checks refuse 0 of 940; none carried ``deleted``.
"""

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, gbif
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


_KEY = "6d27080f-ed47-48e2-90e8-cdebaba11a03"

# Verbatim from the live search index (q=amphibian), trimmed to the fields the adapter reads.
_HIT = {
    "key": _KEY,
    "doi": "10.15468/crpkfp",
    "title": "RBINS Amphibian collection",
    "type": "OCCURRENCE",
    "description": "<p>The RBINS amphibian collection contains more than 135,000 specimens.</p>",
    "license": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
    "publicationDate": "2025-03-13T12:00:00.000+00:00",
    "modified": "2025-03-13T02:50:59.425+00:00",
    "keywords": ["Occurrence", "Specimen", "DaRWIN", "Amphibia"],
    "publishingOrganizationTitle": "Royal Belgian Institute of Natural Sciences",
}

# Verbatim from the live dataset record, trimmed to the fields the adapter reads.
_RECORD = {
    "key": _KEY,
    "doi": "10.15468/crpkfp",
    "title": "RBINS Amphibian collection",
    "type": "OCCURRENCE",
    "description": "<p>The RBINS amphibian collection contains more than 135,000 specimens.</p>",
    "license": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
    "created": "2024-10-09T13:18:17.549+00:00",
    "modified": "2025-03-13T14:50:59.425+00:00",
    "pubDate": "2025-03-13T00:00:00.000+00:00",
    "contacts": [
        {
            "type": "ORIGINATOR",
            "firstName": "Olivier",
            "lastName": "Pauwels",
            "organization": "Royal Belgian Institute for Natural Sciences",
        },
        {
            "type": "METADATA_AUTHOR",
            "firstName": "Stijn",
            "lastName": "Cooleman",
            "organization": "Belgian Biodiversity Platform",
        },
        {
            "type": "ADMINISTRATIVE_POINT_OF_CONTACT",
            "firstName": "Patrick",
            "lastName": "Semal",
            "organization": "Royal Belgian Institute for Natural Sciences",
        },
    ],
    "keywordCollections": [
        {"thesaurus": "GBIF Dataset Type Vocabulary", "keywords": ["Occurrence"]},
        {"thesaurus": "N/A", "keywords": ["DaRWIN", "Amphibia"]},
    ],
    "endpoints": [
        {
            "type": "DWC_ARCHIVE",
            "url": "https://ipt.naturalsciences.be/archive.do?r=be_rbins_vertebrates_amphibia",
        },
        {
            "type": "EML",
            "url": "https://ipt.naturalsciences.be/eml.do?r=be_rbins_vertebrates_amphibia",
        },
    ],
}

_DELETED_KEY = "9600dc32-664c-4b8a-a9bc-51fb21984106"
# Verbatim from the live record of a deleted dataset, trimmed.
_DELETED = {
    "key": _DELETED_KEY,
    "doi": "10.15468/xwk5ub",
    "title": "Grassland Dynamics at eLTER site Hochschwab, Austria, 2001, 2003 and 2010",
    "type": "SAMPLING_EVENT",
    "deleted": "2026-09-21T07:04:47.560+00:00",
    "endpoints": [
        {"type": "DWC_ARCHIVE", "url": "https://ipt.osca.science/archive.do?r=at_hoch_veg"},
        {"type": "EML", "url": "https://ipt.osca.science/eml.do?r=at_hoch_veg"},
    ],
}

_NO_HITS = {"offset": 0, "limit": 10, "endOfRecords": True, "count": 0, "results": [], "facets": []}


def _page(*hits: object, count: object = 1) -> dict:
    return {"offset": 0, "limit": 10, "endOfRecords": True, "count": count, "results": list(hits)}


def _serving(body: object, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    )


_SEARCH_BROKEN = r"\[UpstreamUnavailableError\] GBIF dataset search returned an unparseable 200 body after 3 tries"
_RECORD_BROKEN = (
    r"\[UpstreamUnavailableError\] GBIF dataset returned an unparseable 200 body after 3 tries"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"count": 5},  # no result list: was read as 5 hits and no records
        {"results": []},  # no count: was read as 0
        {"count": "5", "results": []},
        {"count": True, "results": []},
        {"count": 1, "results": None},
        {"count": 1, "results": {"key": _KEY}},
        _page({"title": "no key"}),
        _page({**_HIT, "key": ""}),
        _page({**_HIT, "key": 7}),
        _page("x"),
        _page({**_HIT, "keywords": "Occurrence"}),  # was read as nine one-letter subjects
        _page({**_HIT, "title": 7}),
    ],
)
async def test_a_broken_search_answer_is_an_error_not_no_hits(body):
    async with _serving(body) as c:
        with pytest.raises(UpstreamUnavailableError, match=f"^{_SEARCH_BROKEN}"):
            await gbif.search(c, "amphibian")
    # Positive controls: the live no-hit answer is zero hits, a real page is read.
    async with _serving(_NO_HITS) as c:
        assert await gbif.search(c, "zzqqxx") == (0, [])
    async with _serving(_page(_HIT, count=2136)) as c:
        total, recs = await gbif.search(c, "amphibian")
    assert total == 2136 and [r.id for r in recs] == [f"gbif:{_KEY}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {**_RECORD, "key": None},  # was read as the record "gbif:"
        {**_RECORD, "contacts": "Olivier Pauwels"},
        {**_RECORD, "contacts": [None]},
        {**_RECORD, "endpoints": [{"type": "DWC_ARCHIVE", "url": 7}]},
        {**_RECORD, "keywordCollections": [{"keywords": "Occurrence"}]},
    ],
)
async def test_a_broken_record_is_an_error_not_a_record(body):
    async with _serving(body) as c:
        with pytest.raises(UpstreamUnavailableError, match=f"^{_RECORD_BROKEN}"):
            await gbif.resolve(c, f"gbif:{_KEY}")
    async with _serving(_RECORD) as c:  # positive control: the real record is read
        assert (await gbif.resolve(c, f"gbif:{_KEY}")).id == f"gbif:{_KEY}"


# Every type a JSON value can take, one of each.
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(doc: dict, path: tuple, value: object) -> dict:
    rec = copy.deepcopy(doc)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


@pytest.mark.parametrize("doc", [_HIT, _RECORD], ids=["search-hit", "record"])
def test_no_wrong_typed_field_escapes_as_a_bare_error(doc):
    """Walks every field of a real hit and a real record with every JSON type: each must
    be refused by the check or read cleanly, so a field the reader starts using without
    the check fails here (the datacite lesson of 2026-10-01)."""
    # Positive control: the real shapes pass the check and read whole.
    gbif._check_dataset(doc)
    gbif._check_hits(_page(doc))
    assert gbif._normalize(doc).title == "RBINS Amphibian collection"
    escaped = []
    for path in _paths(doc):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(doc, path, value)
            try:
                gbif._check_dataset(rec)
            except _http.UpstreamEnvelopeError:
                continue
            try:
                gbif._normalize(rec)
                gbif._archive_files(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


def test_the_checks_refuse_with_a_message_naming_gbif():
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no GBIF dataset in \{'key': 7\}$"):
        gbif._check_dataset({"key": 7})
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no GBIF dataset list in \{\}$"):
        gbif._check_hits({})
    gbif._check_dataset(_RECORD)  # positive control
    gbif._check_hits(_NO_HITS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid", ["gbif:notauuid", "gbif:", "gbif:../search", "gbif:6d27080f-ed47-48e2-90e8"]
)
async def test_a_malformed_key_is_not_found_before_the_network(rid):
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        return httpx.Response(400, text="Invalid UUID string: notauuid")  # GBIF's answer

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(
            NotFoundError,
            match=rf"^\[NotFoundError\] malformed GBIF dataset key '{rid}' \(not a UUID\)$",
        ):
            await gbif.resolve(c, rid)
        assert sent == []
        # Positive control: a well-formed key reaches GBIF, in its canonical form.
        with pytest.raises(UpstreamUnavailableError, match="HTTP 400"):
            await gbif.resolve(c, f"gbif:{_KEY.upper()}")
    assert [str(r.url) for r in sent] == [f"https://api.gbif.org/v1/dataset/{_KEY}"]


@pytest.mark.asyncio
async def test_a_deleted_dataset_is_not_found():
    async with _serving(_DELETED) as c:
        with pytest.raises(
            NotFoundError,
            match=rf"^\[NotFoundError\] GBIF dataset '{_DELETED_KEY}' was deleted on 2026-09-21T07:04:47\.560\+00:00$",
        ):
            await gbif.resolve(c, f"gbif:{_DELETED_KEY}")
    # Positive control: the same record without the deletion stamp is a live dataset.
    live = {k: v for k, v in _DELETED.items() if k != "deleted"}
    async with _serving(live) as c:
        r = await gbif.resolve(c, f"gbif:{_DELETED_KEY}")
    assert [f.url for f in r.files] == ["https://ipt.osca.science/archive.do?r=at_hoch_veg"]


@pytest.mark.asyncio
async def test_a_real_record_reads_whole():
    async with _serving(_RECORD) as c:
        r = await gbif.resolve(c, f"gbif:{_KEY}")
    assert r.creators == [Creator(name="Olivier Pauwels"), Creator(name="Stijn Cooleman")]
    assert r.subjects == ["Occurrence", "DaRWIN", "Amphibia"]
    assert r.year == 2025 and r.license == "CC-BY-NC-4.0" and r.access == "open"
    assert [(f.name, f.url) for f in r.files] == [
        (f"{_KEY}.dwca.zip", _RECORD["endpoints"][0]["url"])  # type: ignore[index]
    ]


# --- live (opt-in) ------------------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_a_deleted_dataset_is_not_found():
    """GBIF keeps a deleted dataset's record; take one from its own deleted list, so the
    test does not depend on one dataset staying deleted."""
    async with httpx.AsyncClient(timeout=60) as c:
        listed = (
            await c.get("https://api.gbif.org/v1/dataset/deleted", params={"limit": 1})
        ).json()
        key = listed["results"][0]["key"]
        with pytest.raises(
            NotFoundError, match=rf"^\[NotFoundError\] GBIF dataset '{key}' was deleted on 20"
        ):
            await gbif.resolve(c, f"gbif:{key}")
        # Positive control: a live dataset resolves with its archive.
        r = await gbif.resolve(c, f"gbif:{_KEY}")
    assert r.id == f"gbif:{_KEY}" and r.files


@_live_only
@pytest.mark.asyncio
async def test_live_gbif_refuses_a_non_uuid_key_with_400():
    """The premise of resolving a malformed key locally: GBIF answers it with 400 (an
    outage to `_http`), not 404."""
    async with httpx.AsyncClient(timeout=60) as c:
        resp = await c.get("https://api.gbif.org/v1/dataset/notauuid")
        assert resp.status_code == 400 and "Invalid UUID" in resp.text
        with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] malformed GBIF dataset key"):
            await gbif.resolve(c, "gbif:notauuid")


@_live_only
@pytest.mark.asyncio
async def test_live_a_full_search_page_passes_the_check():
    """The check refuses none of a full live page at the largest size the adapter asks."""
    async with httpx.AsyncClient(timeout=120) as c:
        total, recs = await gbif.search(c, "amphibian", size=gbif.MAX_SIZE)
    assert total > gbif.MAX_SIZE and len(recs) == gbif.MAX_SIZE
