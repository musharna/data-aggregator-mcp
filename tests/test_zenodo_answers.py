"""Each Zenodo answer beside how the adapter must read it.

Probed live 2026-10-02 (zenodo.org/api/records, anonymous):
- A page over 25 records is refused: ``400 {"field": "size", "messages": ["Page size
  cannot be greater than 25. Please use authenticated requests to increase the limit to
  100."]}``; 25 is answered. ``MAX_SIZE`` was 50, so every search for 26-50 failed.
- A query with no hits is ``200 {"hits": {"hits": [], "total": 0}, "aggregations": ...}``;
  an unknown id is ``404 {"status": 404, "message": "The persistent identifier does not
  exist."}``. A real answer always carries ``hits.hits`` and an int ``hits.total``.
- 250 records from 10 queries at random pages: every field the adapter reads came back
  at the type below or absent (description 4%, orcid 52%, grants 77%, keywords 43%,
  license 3%, related_identifiers 67% absent). Default search already returns latest
  versions only (25/25 ``is_last``; 111,767 hits vs 128,724 with ``all_versions``).
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, zenodo
from data_aggregator_mcp._cache import TTLCache
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(zenodo, "_SEARCH_CACHE", TTLCache(maxsize=256, ttl=600))


# Verbatim from the live API (record 1491532), trimmed to the fields the adapter reads.
_FULL = {
    "id": 1491532,
    "doi": "10.5281/zenodo.1491532",
    "files": [
        {
            "key": "D7.8 Data Management Plan v1.0-final.pdf",
            "size": 592306,
            "checksum": "md5:9d6ab24c9bd73f903eccd48c51ec4908",
            "links": {
                "self": "https://zenodo.org/api/records/1491532/files/"
                "D7.8%20Data%20Management%20Plan%20v1.0-final.pdf/content"
            },
        }
    ],
    "stats": {"views": 832, "downloads": 754},
    "metadata": {
        "title": "CLARITY D7.8 Data Management Plan",
        "publication_date": "2017-11-30",
        "description": "<p>This report is the first deliverable of Task 7.3",
        "resource_type": {"title": "Project deliverable", "type": "publication"},
        "creators": [
            {"name": "Dihé, Pascal", "affiliation": "cismet GmbH", "orcid": "0000-0002-0299-9116"}
        ],
        "grants": [
            {
                "code": "730355",
                "funder": {"name": "European Commission", "acronym": "EC"},
                "title": "Integrated Climate Adaptation Service Tools",
            }
        ],
        "keywords": ["Data Management Plan", "Open Research Data Pilot"],
        "license": {"id": "cc-by-sa-4.0"},
        "access_right": "open",
        "related_identifiers": [
            {
                "identifier": "https://csis.myclimateservice.eu/",
                "relation": "isReferencedBy",
                "scheme": "url",
            }
        ],
        "relations": {"version": [{"index": 0, "is_last": True}]},
    },
}


def _hits(*records: dict, total: int | None = None) -> dict:
    n = len(records) if total is None else total
    return {"hits": {"hits": list(records), "total": n}, "aggregations": {}}


def _client(body: object, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_search_asks_for_no_more_than_zenodo_answers_anonymously():
    """A size-50 search sent size=50 and Zenodo refused it with a 400."""
    seen: list[httpx.Request] = []
    async with _client(_hits(_FULL, total=111767), seen) as c:
        total, recs = await zenodo.search(c, "climate", size=50)
        await zenodo.search(c, "climate", size=50, offset=30)
        await zenodo.search(c, "climate", size=10)
    assert [dict(r.url.params) for r in seen] == [
        {"q": "climate", "size": "25"},
        {"q": "climate", "size": "25", "page": "2"},  # records 25-49; drop 25-29
        {"q": "climate", "size": "10"},
    ]
    # Positive control: the answer reads whole.
    assert total == 111767 and [r.id for r in recs] == ["zenodo:1491532"]


_MALFORMED = [
    {},
    {"hits": None},
    {"hits": {"hits": []}},
    {"hits": {"hits": [], "total": "0"}},
    {"hits": {"hits": [], "total": True}},
    {"hits": {"hits": _FULL, "total": 1}},
    {"hits": {"hits": [{"metadata": {}}], "total": 1}},
    {"hits": {"hits": [{**_FULL, "id": "1491532"}], "total": 1}},
    {"hits": {"hits": [{**_FULL, "metadata": None}], "total": 1}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED)
async def test_a_malformed_answer_is_upstream_trouble_not_an_empty_one(body):
    """A 200 without ``hits.hits``/``hits.total`` read as zero hits (or the page length
    as the total), and a record without its id or metadata escaped as a bare error."""
    async with _client(body) as c:
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await zenodo.search(c, "climate")
    # resolve gets the malformed record itself, or the whole body when there is none.
    listed = (body.get("hits") or {}).get("hits")
    record = listed[0] if isinstance(listed, list) and listed else body
    async with _client(record) as c:
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await zenodo.resolve(c, "zenodo:1491532")
    # Positive control: the live no-hit answer is zero hits, and a record resolves.
    async with _client(_hits()) as c:
        assert await zenodo.search(c, "climate") == (0, [])
    async with _client(_FULL) as c:
        assert (await zenodo.resolve(c, "zenodo:1491532")).doi == "10.5281/zenodo.1491532"


def test_the_live_record_reads_whole():
    zenodo._check_record(_FULL)
    r = zenodo._normalize(_FULL)
    assert r.kind == "publication" and r.year == 2017 and r.license == "cc-by-sa-4.0"
    assert r.creators == [Creator(name="Dihé, Pascal", orcid="0000-0002-0299-9116")]
    assert [(f.funder, f.award) for f in r.funding] == [("European Commission", "730355")]
    assert [(lk.rel, lk.target_id) for lk in r.links] == [
        ("is_referenced_by", "https://csis.myclimateservice.eu/")
    ]
    [f] = r.files
    assert f.checksum == "md5:9d6ab24c9bd73f903eccd48c51ec4908" and f.size == 592306
    assert r.metrics is not None and (r.metrics.views, r.metrics.downloads) == (832, 754)
    assert r.is_latest is True and r.subjects == [
        "Data Management Plan",
        "Open Research Data Pilot",
    ]


_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(path, value):
    rec = copy.deepcopy(_FULL)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Walk every field of the live record with every wrong JSON type: each must be
    refused by the record check or read cleanly, so a field read without the check
    fails here."""
    # Positive control: the full record passes the check and reads whole.
    zenodo._check_record(_FULL)
    assert zenodo._normalize(_FULL).funding[0].funder == "European Commission"
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(path, value)
            try:
                zenodo._check_record(rec)
            except _http.UpstreamEnvelopeError:
                continue
            try:
                zenodo._normalize(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


@pytest.mark.asyncio
async def test_an_unknown_id_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"status": 404, "message": "The persistent identifier does not exist."}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(NotFoundError, match="Zenodo has no record id='99999999999'"):
            await zenodo.resolve(c, "zenodo:99999999999")
