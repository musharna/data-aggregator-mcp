"""Each DataCite answer beside how the adapter must read it.

Probed live 2026-10-01 (api.datacite.org):
- 90,315 records (``_exists_:creators AND NOT _exists_:creators.name``) name a creator
  only by ``givenName``/``familyName``; the ``name`` key is absent. 10.48660/26100072:
  ``{"nameType": "Personal", "givenName": "Yannick", "familyName": "Kluth"}``.
- A random sample of 800 records: 8 of 656 with descriptions list another type first
  while an ``Abstract`` exists (7 ``Other``, 1 ``SeriesInformation``), e.g.
  10.57761/v29k-9n03 opens with a contact block; 1 of 294 multi-title records lists an
  ``AlternativeTitle`` before its main (untyped) title (10.26240/heal.ntua.2621).
- An unknown DOI is 404; a search with no hits is ``200 {"data": [], "meta":
  {"total": 0, ...}}``. A real answer always carries ``data`` and ``meta.total``.
"""

import httpx
import pytest

from data_aggregator_mcp import _http, datacite
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import Creator


def _record(**attributes) -> dict:
    return {
        "id": "10.48660/26100072",
        "type": "dois",
        "attributes": {
            "doi": "10.48660/26100072",
            "titles": [{"title": "A record"}],
            "types": {"resourceTypeGeneral": "Dataset"},
            **attributes,
        },
        "relationships": {"client": {"data": {"id": "tdl.tdl", "type": "clients"}}},
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(body: object, seen: list[str] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_a_creator_named_only_by_given_and_family_name_keeps_the_name():
    """The 90,315 records above came back with ``Creator(name="")``: a nameless
    author. DataCite writes ``name`` as "Family, Given" ("Heim, Arnold")."""
    r = datacite._normalize(
        _record(
            creators=[
                {"nameType": "Personal", "givenName": "Yannick", "familyName": "Kluth"},
                {"nameType": "Personal", "familyName": "Kovachik"},
                {"nameType": "Personal", "givenName": "Andrew", "name": None},
                {"nameType": "Personal", "affiliation": ["U"]},  # no name at all
                {"name": "Heim, Arnold", "givenName": "Arnold", "familyName": "Heim"},  # control
            ]
        )
    )
    assert r.creators == [
        Creator(name="Kluth, Yannick"),
        Creator(name="Kovachik"),
        Creator(name="Andrew"),
        Creator(name="Heim, Arnold"),
    ]


def test_a_nameless_creator_with_an_orcid_is_kept_for_the_orcid():
    orcid = {
        "nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
        "nameIdentifierScheme": "ORCID",
    }
    r = datacite._normalize(_record(creators=[{"nameIdentifiers": [orcid]}, {"name": "B"}]))
    assert r.creators == [Creator(name="", orcid="0000-0002-1825-0097"), Creator(name="B")]


def test_the_description_is_the_abstract_when_one_is_listed():
    """10.57761/v29k-9n03 lists a contact block (``Other``) first; 10.83268/jdep.2026.1252849
    a journal series line. The first entry was reported as the dataset's description."""
    contact = {"descriptionType": "Other", "description": "# Contact itemresponsewarehouse@"}
    series = {"descriptionType": "SeriesInformation", "description": "Bi-quarterly Journal"}
    abstract = {"descriptionType": "Abstract", "description": "The Item Response Warehouse"}
    methods = {"descriptionType": "Methods", "description": "Tables have been harmonized"}
    cases = [
        ([contact, abstract, methods], "The Item Response Warehouse"),
        ([series, abstract], "The Item Response Warehouse"),
        ([contact, methods], "# Contact itemresponsewarehouse@"),  # no abstract: the first
        ([{"description": "Untyped."}], "Untyped."),  # control
    ]
    for descriptions, expected in cases:
        assert datacite._normalize(_record(descriptions=descriptions)).description == expected


def test_the_title_is_the_main_title_when_one_is_listed():
    """10.26240/heal.ntua.2621 lists its Greek ``AlternativeTitle`` before the main title."""
    alt = {"titleType": "AlternativeTitle", "title": "Το όριο προς τη θάλασσα"}
    main = {"title": "The boundary to the sea"}
    sub = {"titleType": "Subtitle", "title": "A study"}
    assert datacite._normalize(_record(titles=[alt, main])).title == "The boundary to the sea"
    assert datacite._normalize(_record(titles=[alt])).title == "Το όριο προς τη θάλασσα"
    assert datacite._normalize(_record(titles=[main, sub])).title == "The boundary to the sea"


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"title": "Something went wrong"}]},
        {"data": []},
        {"data": {"attributes": "x"}},
        {"data": {"attributes": {"titles": [{"title": "no doi"}]}}},
        {"data": _record(titles="A record")},
        {"data": _record(creators=[{"name": ["A", "B"]}])},
        {"data": _record(updated=20260101)},
        {"data": _record(url=["https://example.org/landing"])},
    ],
)
@pytest.mark.asyncio
async def test_a_record_answer_without_a_record_is_a_malformed_answer(body):
    """A 200 without a ``data`` record object was reported as "DataCite has no DOI" /
    "malformed response" NotFoundError (an outage read as absence), escaped as a bare
    ``AttributeError``/``TypeError``/pydantic error, or resolved to ``datacite:None``."""
    async with _client(body) as c:
        with pytest.raises(UpstreamUnavailableError, match="200 body.*no DataCite record in"):
            await datacite.resolve(c, "datacite:10.48660/26100072")
    async with _client({"data": _record()}) as c:
        r = await datacite.resolve(c, "datacite:10.48660/26100072")  # positive control
    assert r.id == "datacite:10.48660/26100072" and r.title == "A record"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": None, "meta": {"total": 0}},
        {"data": {}, "meta": {"total": 0}},
        {"data": [1], "meta": {"total": 1}},
        {"data": [_record(titles="A record")], "meta": {"total": 1}},
        {"data": []},
        {"data": [], "meta": {"total": "many"}},
        {"data": [_record()], "meta": {"total": True}},
    ],
)
@pytest.mark.asyncio
async def test_a_search_answer_without_its_records_and_total_is_a_malformed_answer(body):
    """A 200 lacking ``data`` read as zero hits; a missing ``meta.total`` became the
    page length, ``True`` became 1, and a non-number or a non-record entry escaped as
    a bare ``ValueError``/``AttributeError``."""
    async with _client(body) as c:
        with pytest.raises(UpstreamUnavailableError, match="200 body.*no DataCite record list in"):
            await datacite.search(c, "kluth")
    async with _client({"data": [_record()], "meta": {"total": 7}}) as c:
        total, recs = await datacite.search(c, "kluth")  # positive control
    assert total == 7 and [r.id for r in recs] == ["datacite:10.48660/26100072"]
    async with _client({"data": [], "meta": {"total": 0, "totalPages": 0, "page": 1}}) as c:
        assert await datacite.search(c, "zzqqxxnohitxyz") == (0, [])  # the live no-hit answer


# Every field `_normalize` reads, nested as DataCite serves it (live types 2026-10-01:
# counts and publicationYear int, relationships.client.data an object with a str id,
# nameIdentifiers a list of objects).
_FULL = {
    "id": "10.1234/full",
    "type": "dois",
    "attributes": {
        "doi": "10.1234/full",
        "titles": [{"title": "T", "titleType": "AlternativeTitle"}],
        "descriptions": [{"description": "D", "descriptionType": "Abstract"}],
        "creators": [
            {
                "name": "Doe, J.",
                "givenName": "J.",
                "familyName": "Doe",
                "nameIdentifiers": [
                    {"nameIdentifier": "0000-0002-1825-0097", "nameIdentifierScheme": "ORCID"}
                ],
            }
        ],
        "subjects": [{"subject": "s"}],
        "rightsList": [{"rights": "CC0", "rightsUri": "https://x", "rightsIdentifier": "cc0-1.0"}],
        "fundingReferences": [{"funderName": "F", "awardNumber": "1", "awardTitle": "A"}],
        "relatedIdentifiers": [{"relationType": "IsPartOf", "relatedIdentifier": "10.1/y"}],
        "types": {"resourceTypeGeneral": "Dataset"},
        "publicationYear": 2023,
        "updated": "2024-01-01T00:00:00Z",
        "url": "https://example.org/landing",
        "citationCount": 1,
        "viewCount": 2,
        "downloadCount": 3,
    },
    "relationships": {"client": {"data": {"id": "tdl.tdl", "type": "clients"}}},
}
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(path, value):
    import copy

    rec = copy.deepcopy(_FULL)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Review of #168: `_is_record` checked fewer fields than `_normalize` reads
    (creators[].nameIdentifiers, the relationships chain, the usage counts), so a
    wrong-typed one still escaped as a bare AttributeError / pydantic error. This walks
    every field of a full record and every wrong type: each must be refused by the check
    or normalize cleanly, so a field the reader adds without the check fails here."""
    full = datacite._normalize(_FULL)  # positive control: the full record reads whole
    assert full.creators == [Creator(name="Doe, J.", orcid="0000-0002-1825-0097")]
    assert full.source == "tdl.tdl" and full.metrics is not None and full.metrics.downloads == 3
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(path, value)
            try:
                datacite._check_record({"data": rec})
            except _http.UpstreamEnvelopeError:
                continue
            try:
                datacite._normalize(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []
