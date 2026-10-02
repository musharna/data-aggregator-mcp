"""What the GWAS Catalog v2 API answers, and how each answer is read.

Shapes probed live 2026-10-02 (05:19-05:45 EDT): a trait with no studies and a page
past the last one both answer 200 with ``page`` and NO ``_embedded`` key (verbatim in
``fixtures/gwas_v2_search_empty.json`` and ``gwas_v2_search_past_end.json``); an unknown
study, a lower-case accession and an unknown publication answer 404. In 14 pages of
up to 100 studies (1,365 rows, ~0.6% of the 231,865 in the Catalog) every ``accession_id``
was ``GCST`` + digits, every ``pubmed_id`` an int and every ``disease_trait`` a string;
in 23 publications every ``title`` and ``publication_date`` was a string.
"""

import copy
import json
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import _http, gwas
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_FIX = Path(__file__).parent / "fixtures"
_SEARCH = json.loads((_FIX / "gwas_v2_search.json").read_text())  # asthma, size 2
_EMPTY = json.loads((_FIX / "gwas_v2_search_empty.json").read_text())
_PAST_END = json.loads((_FIX / "gwas_v2_search_past_end.json").read_text())  # page 50
_RECORD = json.loads((_FIX / "gwas_v2_study.json").read_text())  # GCST000028
_PUBLICATION = json.loads((_FIX / "gwas_v2_publication.json").read_text())
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(*bodies) -> httpx.AsyncClient:
    """Answer each request with the next body (the last one repeats), all HTTP 200."""
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _page(studies, *, total, number=0, size=10, embedded=True) -> dict:
    body = {"page": {"size": size, "totalElements": total, "totalPages": 0, "number": number}}
    if embedded:
        body["_embedded"] = {"studies": studies}
    return body


@pytest.mark.asyncio
async def test_a_trait_with_no_studies_is_zero_hits():
    async with _client(_EMPTY) as c:
        assert await gwas.search(c, "Type 2 diabetes mellitus xyz", size=3) == (0, [])


@pytest.mark.asyncio
async def test_a_page_past_the_last_is_empty_with_the_real_total():
    async with _client(_PAST_END) as c:
        assert await gwas.search(c, "asthma", size=10, offset=500) == (87, [])


@pytest.mark.asyncio
async def test_a_page_starting_exactly_at_the_total_is_empty():
    # 20 studies, size 10: page 2 starts at row 20, which the total does not reach.
    async with _client(_page([], total=20, number=2, embedded=False)) as c:
        assert await gwas.search(c, "asthma", size=10, offset=20) == (20, [])
    # ... but one study more and that page has a row, so its list cannot be missing.
    async with _client(_page([], total=21, number=2, embedded=False)) as c:
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await gwas.search(c, "asthma", size=10, offset=20)


_BROKEN_PAGES = {
    "empty object": {},
    "error envelope": {"errorCode": 500, "error": "Internal Server Error"},
    "rows due, list missing": _page([], total=87, embedded=False),
    "no studies key": {"_embedded": {}, "page": _SEARCH["page"]},
    "_embedded not an object": {"_embedded": [], "page": _SEARCH["page"]},
    "studies not a list": {"_embedded": {"studies": {}}, "page": _SEARCH["page"]},
    "no page": {"_embedded": _SEARCH["_embedded"]},
    "page not an object": {"_embedded": _SEARCH["_embedded"], "page": 87},
    "total null": _page(_SEARCH["_embedded"]["studies"], total=None),
    "total a string": _page(_SEARCH["_embedded"]["studies"], total="87"),
    "total a bool": _page(_SEARCH["_embedded"]["studies"], total=True),
    "a row not an object": _page(["GCST90480249"], total=1),
    "a row without accession": _page([{"disease_trait": "Asthma"}], total=1),
    "a row with a non-GCST accession": _page([{"accession_id": "ACC000"}], total=1),
    "a row with an accession and a tail": _page([{"accession_id": "GCST1/x"}], total=1),
    "a pubmed_id string": _page([{"accession_id": "GCST1", "pubmed_id": "../x"}], total=1),
    "a pubmed_id bool": _page([{"accession_id": "GCST1", "pubmed_id": True}], total=1),
    "a trait not a string": _page([{"accession_id": "GCST1", "disease_trait": 7}], total=1),
}


@pytest.mark.parametrize("body", _BROKEN_PAGES.values(), ids=_BROKEN_PAGES.keys())
@pytest.mark.asyncio
async def test_a_broken_search_answer_is_upstream_trouble_not_zero_hits(body):
    """A 200 without the study list, its total, or with a row the reader cannot read was
    read as zero hits (or escaped as a bare TypeError / pydantic error). The real
    answer beside it is still read whole."""
    async with _client(_SEARCH) as c:
        assert (await gwas.search(c, "asthma", size=2))[0] == 87
    async with _client(body) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GWAS Catalog search returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(.no GWAS Catalog study list in ",
        ):
            await gwas.search(c, "asthma", size=2)


_BROKEN_STUDIES = {
    "empty object": {},
    "error envelope": {"errorCode": 500, "error": "Internal Server Error"},
    "accession null": dict(_RECORD, accession_id=None),
    "accession not GCST": dict(_RECORD, accession_id="../evil"),
    "pubmed_id a path": dict(_RECORD, pubmed_id="../../studies"),
    "trait a list": dict(_RECORD, disease_trait=["Type 2 diabetes"]),
}


@pytest.mark.parametrize("body", _BROKEN_STUDIES.values(), ids=_BROKEN_STUDIES.keys())
@pytest.mark.asyncio
async def test_a_broken_study_is_upstream_trouble_not_a_missing_study(body):
    """A 200 `{}` was "GWAS Catalog has no study" (NotFoundError), and a wrong-typed
    field escaped bare or went into the publication URL. Only a 404 is "no study"."""
    async with _client(_RECORD, _PUBLICATION) as c:
        assert (await gwas.resolve(c, "gwas:GCST000028")).year == 2007
    async with _client(body) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GWAS Catalog resolve returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(.no GWAS Catalog study in ",
        ):
            await gwas.resolve(c, "gwas:GCST000028")


@pytest.mark.asyncio
async def test_only_a_404_is_no_such_study():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": 404, "error": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] GWAS Catalog has no study GCST999999$"
        ):
            await gwas.resolve(c, "gwas:GCST999999")


@pytest.mark.parametrize(
    "field,value", [("title", 7), ("title", ["x"]), ("publication_date", 2007)]
)
@pytest.mark.asyncio
async def test_a_wrong_typed_publication_is_a_failed_lookup(field, value):
    """A publication with a wrong-typed field escaped as a bare TypeError (an int date
    sliced) or pydantic error out of resolve; now it is a failed lookup, recorded."""
    async with _client(_RECORD, dict(_PUBLICATION, **{field: value})) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r.title == "Type 2 diabetes" and r.year is None
    assert list(r.errors) == ["publication"]
    assert r.errors["publication"].startswith(
        "UpstreamUnavailableError: [UpstreamUnavailableError] GWAS Catalog publication returned an unparseable 200 "
        'body after 3 tries: UpstreamEnvelopeError("no GWAS Catalog publication in '
        "{'pubmed_id': '17463246', "
    )


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(doc, path, value):
    doc = copy.deepcopy(doc)
    *parents, last = path
    node = doc
    for p in parents:
        node = node[p]
    node[last] = value
    return doc


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Every field of a real study and publication, set to every wrong JSON type: the
    check refuses it, or the reader reads it cleanly. A field the reader starts to use
    without the check fails here."""
    # Positive control: the real answers pass their checks and read whole.
    gwas._check_study(_RECORD)
    gwas._check_publication(_PUBLICATION)
    assert gwas._normalize(_RECORD, _PUBLICATION).year == 2007
    escaped = []
    for doc, check, read in (
        (_RECORD, gwas._check_study, lambda d: gwas._normalize(d, _PUBLICATION)),
        (_PUBLICATION, gwas._check_publication, lambda d: gwas._normalize(_RECORD, d)),
    ):
        for path in _paths(doc):
            for value in _WRONG if path else []:
                bad = _with(doc, path, value)
                try:
                    check(bad)
                except _http.UpstreamEnvelopeError:
                    continue
                try:
                    read(bad)
                except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                    escaped.append((path, value, type(exc).__name__))
    assert escaped == []


def test_the_checks_accept_every_probed_live_shape():
    """The sampled live rows (stated in the module docstring) as the checks see them: a
    GCST accession, an int PMID, a string trait; and a publication of strings."""
    gwas._check_page(_SEARCH, first=0)
    gwas._check_page(_EMPTY, first=0)
    gwas._check_page(_PAST_END, first=500)
    gwas._check_study(_RECORD)
    gwas._check_study({"accession_id": "gcst1"})  # absent fields are fine
    gwas._check_study(dict(_RECORD, pubmed_id=None, disease_trait=None))
    gwas._check_publication({})
    with pytest.raises(_http.UpstreamEnvelopeError, match="^no GWAS Catalog study in"):
        gwas._check_study(["GCST1"])
    with pytest.raises(_http.UpstreamEnvelopeError, match="^no GWAS Catalog publication in"):
        gwas._check_publication({"title": None, "publication_date": 1})
