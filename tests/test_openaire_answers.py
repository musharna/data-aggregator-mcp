"""How the OpenAIRE adapter reads what the Graph API answers, broken answers included.

Live shapes (api.openaire.eu/graph/v1/researchProducts, 2026-10-02): a search with no
match answers 200 ``{"header": {"numFound": 0, ...}, "results": []}``; a page past the
last answers the real ``numFound`` and ``results: []``; an unknown or merged-away id
answers 404 ``{"message": "Research product with id: ... not found", ...}``. In 1,203
search hits (six first pages and six later pages of 100) and 15 single entities, every
field ``_normalize_openaire`` reads had the type ``_FULL`` gives it, or was null.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, openaire
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator
from tests.test_openaire import live_only


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


# A live hit (search "Phelipanche aegyptiaca", 2026-10-02), trimmed to two of each list.
_FULL = {
    "id": "doi_dedup___::d1aa1aac03810099b29c270cba83da9d",
    "type": "publication",
    "mainTitle": "Molecular convergence of the parasitic plant species Cuscuta reflexa and "
    "Phelipanche aegyptiaca",
    "publicationDate": "2012-03-30",
    "bestAccessRight": {
        "code": "c_14cb",
        "label": "CLOSED",
        "scheme": "http://vocabularies.coar-repositories.org/documentation/access_rights/",
    },
    "pids": [
        {"scheme": "doi", "value": "10.1007/s00425-012-1626-x"},
        {"scheme": "pmid", "value": "22460777"},
    ],
    "authors": [
        {"fullName": "Jan, Rehker", "name": "Jan", "surname": "Rehker", "rank": 1, "pid": None},
        {
            "fullName": "Magdalena, Lachnit",
            "name": "Magdalena",
            "surname": "Lachnit",
            "rank": 2,
            "pid": None,
        },
    ],
    "descriptions": ["The parasitic plant species <i>Cuscuta reflexa</i> and Phelipanche"],
    "subjects": [
        {"subject": {"scheme": "FOS", "value": "0301 basic medicine"}, "provenance": None},
        {"subject": {"scheme": "keyword", "value": "Nicotiana"}, "provenance": None},
    ],
    "instances": [
        {
            "pids": [{"scheme": "doi", "value": "10.1007/s00425-012-1626-x"}],
            "license": "Springer TDM",
            "type": "Article",
            "urls": ["https://doi.org/10.1007/s00425-012-1626-x"],
            "publicationDate": "2012-03-30",
            "refereed": "peerReviewed",
        },
        {
            "pids": [{"scheme": "pmid", "value": "22460777"}],
            "alternateIdentifiers": [{"scheme": "doi", "value": "10.1007/s00425-012-1626-x"}],
            "type": "Article",
            "urls": ["https://pubmed.ncbi.nlm.nih.gov/22460777"],
            "publicationDate": "2014-08-19",
            "refereed": "nonPeerReviewed",
        },
    ],
}
_HEADER = {"numFound": 221, "maxScore": 15.172422, "queryTime": 123, "page": 1, "pageSize": 10}
_NO_HITS = {
    "header": {"numFound": 0, "maxScore": 0.0, "queryTime": 128, "page": 1, "pageSize": 10},
    "results": [],
}
_PAST_THE_END = {"header": {**_HEADER, "page": 30}, "results": []}
_UNKNOWN = {
    "message": "Research product with id: nonsense not found",
    "error": "Not Found",
    "code": 404,
    "timestamp": "2026-10-02T12:00:35.886+00:00",
    "path": "/graph/v1/researchProducts/nonsense?null",
}


def _client(*answers: httpx.Response, seen: list[httpx.Request] | None = None):
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _broken_search_bodies() -> list[dict]:
    keyword_as_string = copy.deepcopy(_FULL)
    keyword_as_string["subjects"] = "Nicotiana"
    return [
        {},  # no header, no result list: was zero hits
        {"header": _HEADER},  # no result list: was zero hits of 221
        {"header": _HEADER, "results": None},  # was zero hits of 221
        {"results": [_FULL]},  # no hit count: was a total of 0 beside a hit
        {"header": {"numFound": None}, "results": [_FULL]},
        {"header": {"numFound": "221"}, "results": [_FULL]},
        {"header": {"numFound": True}, "results": [_FULL]},
        {"header": [], "results": [_FULL]},
        {"header": _HEADER, "results": "none"},
        {"header": _HEADER, "results": [keyword_as_string]},  # was a bare TypeError
        {"header": _HEADER, "results": [{**_FULL, "id": ""}]},  # was the id "openaire:"
        {"header": _HEADER, "results": [{k: v for k, v in _FULL.items() if k != "id"}]},
        {"message": "Internal Server Error", "code": 500},  # an error envelope in a 200
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _broken_search_bodies())
async def test_a_broken_search_answer_is_an_outage_not_no_hits(body):
    # Positive controls: the live no-match and past-the-end answers are empty pages
    # with their own totals, and a full hit reads whole.
    async with _client(httpx.Response(200, json=_NO_HITS)) as c:
        assert await openaire.search(c, "zzqqxxnotaword") == (0, [])
    async with _client(httpx.Response(200, json=_PAST_THE_END)) as c:
        assert await openaire.search(c, "Phelipanche aegyptiaca", offset=290) == (221, [])
    async with _client(httpx.Response(200, json={"header": _HEADER, "results": [_FULL]})) as c:
        total, (rec,) = await openaire.search(c, "Phelipanche aegyptiaca")
    assert total == 221 and rec.subjects == ["0301 basic medicine", "Nicotiana"]

    seen: list[httpx.Request] = []
    async with _client(httpx.Response(200, json=body), seen=seen) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=(
                r"^\[UpstreamUnavailableError\] OpenAIRE returned an unparseable 200 body "
                r"after 3 tries: UpstreamEnvelopeError\(['\"]no OpenAIRE result list in "
            ),
        ):
            await openaire.search(c, "Phelipanche aegyptiaca")
    assert len(seen) == 3  # retried, as a malformed body is


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},  # was an untitled record named after the requested id
        {k: v for k, v in _FULL.items() if k != "id"},  # was named after the requested id
        {**_FULL, "id": None},
        {**_FULL, "id": 7},
        {**_FULL, "authors": "Jan, Rehker"},  # was a bare TypeError
        # An instance's pids as a string was read letter by letter: a bare AttributeError.
        {**_FULL, "pids": [], "instances": [{"pids": "10.1007/s00425-012-1626-x"}]},
        {**_FULL, "bestAccessRight": {"label": 7}},  # was access "unknown"
        {"message": "Internal Server Error", "error": "Internal Server Error", "code": 500},
    ],
)
async def test_a_broken_record_answer_is_an_outage_not_a_record(body, monkeypatch):
    async def _no_links(client, doi):
        return [], None

    async def _no_ids(client, doi):
        return {}, None

    async def _no_fulltext(client, pmcid=None, doi=None):
        from data_aggregator_mcp import fulltext

        return fulltext.FullText()

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_links)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _no_ids)
    monkeypatch.setattr("data_aggregator_mcp.fulltext.find", _no_fulltext)
    oid = "openaire:doi_dedup___::d1aa1aac03810099b29c270cba83da9d"
    # Positive controls: the live record reads; the live 404 is "not found".
    async with _client(httpx.Response(200, json=_FULL)) as c:
        r = await openaire.resolve(c, oid)
    assert (r.id, r.title[:23], r.doi) == (
        oid,
        "Molecular convergence o",
        "10.1007/s00425-012-1626-x",
    )
    async with _client(httpx.Response(404, json=_UNKNOWN)) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] OpenAIRE → HTTP 404: \{\"message\""
        ):
            await openaire.resolve(c, "openaire:nonsense")

    seen: list[httpx.Request] = []
    async with _client(httpx.Response(200, json=body), seen=seen) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=(
                r"^\[UpstreamUnavailableError\] OpenAIRE returned an unparseable 200 body "
                r"after 3 tries: UpstreamEnvelopeError\(['\"]no OpenAIRE record in "
            ),
        ):
            await openaire.resolve(c, oid)
    assert len(seen) == 3


_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]

# A live-shaped hit whose own pids carry no DOI, so the reader falls back to the
# instances' pids (12 of 600 live hits sampled had a DOI only there or nowhere).
_FALLBACK = {**_FULL, "pids": [{"scheme": "pmid", "value": "22460777"}]}


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(base, path, value):
    rec = copy.deepcopy(base)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Every field of a full hit, and of a hit read through its DOI fallback, set to
    every JSON type: the check refuses it, or the hit reads cleanly. A field the reader starts using without the check fails here."""
    # Positive control: the full hit passes and reads whole.
    assert openaire._is_record(_FULL)
    full = openaire._normalize_openaire(_FULL)
    assert (full.id, full.kind, full.year, full.doi, full.license, full.access) == (
        "openaire:doi_dedup___::d1aa1aac03810099b29c270cba83da9d",
        "publication",
        2012,
        "10.1007/s00425-012-1626-x",
        "Springer TDM",
        "closed",
    )
    assert full.creators == [Creator(name="Jan, Rehker"), Creator(name="Magdalena, Lachnit")]
    assert full.description == "The parasitic plant species Cuscuta reflexa and Phelipanche"
    assert openaire._normalize_openaire(_FALLBACK).doi == "10.1007/s00425-012-1626-x"
    escaped, refused = [], 0
    for base, path in [(b, p) for b in (_FULL, _FALLBACK) for p in _paths(b)]:
        if not path:
            continue
        for value in _WRONG:
            rec = _with(base, path, value)
            if not openaire._is_record(rec):
                refused += 1
                continue
            try:
                openaire._normalize_openaire(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []
    assert refused > 0  # the walk reached the check


@pytest.mark.parametrize(
    ("product_type", "kind"),
    [
        ("dataset", "dataset"),  # live: doi_________::55556335c2d8550b3c93b32bc482b008
        ("software", "software"),  # live: openaire____::2168e948062ee3dc11cdf3ce9b216b75
        ("other", "other"),  # live: doi_________::8d87044408b0e2a872c10a3112f56ae1
        ("annotation", "other"),  # not a Graph API type: unknown, not a publication
        (None, "other"),
    ],
)
def test_a_research_product_keeps_its_own_type_as_its_kind(product_type, kind):
    """The entity endpoint serves every research-product type, and every record was
    called a publication: resolving a dataset's id said ``kind="publication"``."""
    # Positive control: a publication is still a publication.
    assert openaire._normalize_openaire(_FULL).kind == "publication"
    assert openaire._normalize_openaire({**_FULL, "type": product_type}).kind == kind


@live_only
@pytest.mark.asyncio
async def test_live_a_dataset_resolves_as_a_dataset():
    """A dataset id from ``type=dataset`` search (2026-10-02); it resolved as a publication."""
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(
            client, "openaire:doi_________::55556335c2d8550b3c93b32bc482b008"
        )
        # Positive control: a publication id still resolves as a publication.
        p = await openaire.resolve(
            client, "openaire:doi_dedup___::d1aa1aac03810099b29c270cba83da9d"
        )
    assert (r.kind, r.doi) == ("dataset", "10.25549/examiner-m10957")
    assert (p.kind, p.doi) == ("publication", "10.1007/s00425-012-1626-x")


@live_only
@pytest.mark.asyncio
async def test_live_empty_pages_are_answers_not_malformed():
    """No match, and a page past the last, are empty result lists the check accepts;
    a full page at the largest size the adapter asks for passes it too."""
    async with httpx.AsyncClient() as client:
        none_total, none = await openaire.search(client, "zzqqxxnotawordqq")
        total, past = await openaire.search(client, "Phelipanche aegyptiaca", size=10, offset=5000)
        big_total, big = await openaire.search(client, "salmon", size=openaire.MAX_SIZE)
    assert (none_total, none) == (0, [])
    assert past == [] and 0 < total < 5000
    assert len(big) == openaire.MAX_SIZE and big_total > openaire.MAX_SIZE


@live_only
@pytest.mark.asyncio
async def test_live_an_unknown_id_is_not_found():
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] OpenAIRE → HTTP 404: "):
            await openaire.resolve(
                client, "openaire:doi_dedup___::00000000000000000000000000000000"
            )
