"""How the data.gov adapter reads what the catalog answers, broken answers included.

Live shapes (catalog.data.gov, 2026-10-02): a search with no match answers 200
``{"results": [], "sort": "relevance"}``; an unknown dataset answers 404
``{"aggregations": null, "results": [], "search_after": null, "total": 0}``. In 3,472
hits from ten 250-row searches and one 1,000-row search, every field ``_normalize``
reads had the type ``_FULL`` gives it, or was absent or null.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, datagov
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator


@pytest.fixture(autouse=True)
def _keyless_and_no_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


# A hit carrying every field the reader uses, each at its live type.
_FULL = {
    "slug": "water-quality",
    "title": "Hit title",
    "description": "Hit description",
    "organization": {"name": "City of Somewhere"},
    "dcat": {
        "title": "Water quality",
        "description": "<p>Samples</p>",
        "publisher": {"name": "Somewhere Water Board"},
        "keyword": ["water", "quality"],
        "theme": ["Environment"],
        "issued": "2020-01-01",
        "modified": "2021-02-03",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "accessLevel": "public",
        "distribution": [
            {
                "downloadURL": "https://example.gov/a.csv",
                "accessURL": "https://example.gov/a",
                "title": "Samples (CSV)",
                "format": "CSV",
                "mediaType": "text/csv",
            }
        ],
    },
}
_NO_HITS = {"results": [], "sort": "relevance"}
_UNKNOWN = {"aggregations": None, "results": [], "search_after": None, "total": 0}


def _client(*answers: httpx.Response, seen: list[httpx.Request] | None = None):
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _broken_search_bodies() -> list[dict]:
    named_by_string = copy.deepcopy(_FULL)
    named_by_string["organization"] = "City of Somewhere"
    keywords_as_string = copy.deepcopy(_FULL)
    keywords_as_string["dcat"]["keyword"] = "water, quality"
    return [
        {},  # no result list at all
        {"results": None, "sort": "relevance"},
        {"results": "none", "sort": "relevance"},
        {"results": [named_by_string]},  # was a bare AttributeError
        {"results": [keywords_as_string]},  # was read letter by letter as subjects
        {"results": [{**_FULL, "slug": ""}]},  # was the id "datagov:"
        {"results": [_FULL], "after": 7},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _broken_search_bodies())
async def test_a_broken_search_answer_is_an_outage_not_no_hits(body):
    # Positive control: the live no-match answer is zero hits, and a full hit reads whole.
    async with _client(httpx.Response(200, json=_NO_HITS)) as c:
        assert await datagov.search(c, "zzqq") == (0, [])
    async with _client(httpx.Response(200, json={"results": [_FULL]})) as c:
        total, (rec,) = await datagov.search(c, "water")
    assert total == 1 and rec.creators == [Creator(name="City of Somewhere")]
    assert rec.subjects == ["water", "quality", "Environment"]

    seen: list[httpx.Request] = []
    async with _client(httpx.Response(200, json=body), seen=seen) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=(
                r"^\[UpstreamUnavailableError\] data\.gov search returned an unparseable "
                r"200 body after 3 tries: .*no data\.gov (dataset list|search cursor) in "
            ),
        ):
            await datagov.search(c, "water")
    assert len(seen) == 3  # retried, as a malformed body is


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"results": "x", "total": 1}, {"results": [{"slug": 7}]}])
async def test_a_broken_dataset_answer_is_an_outage_not_a_missing_dataset(body):
    # Positive controls: a real record reads; the live unknown-id 404 is "not found".
    async with _client(httpx.Response(200, json={"results": [_FULL], "total": 1})) as c:
        assert (await datagov.resolve(c, "datagov:water-quality")).title == "Water quality"
    async with _client(httpx.Response(404, json=_UNKNOWN)) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] data\.gov has no dataset 'nope'$"
        ):
            await datagov.resolve(c, "datagov:nope")

    async with _client(httpx.Response(200, json=body)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=(
                r"^\[UpstreamUnavailableError\] data\.gov resolve returned an unparseable "
                r"200 body after 3 tries: .*no data\.gov dataset list in "
            ),
        ):
            await datagov.resolve(c, "datagov:water-quality")


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
    """Every field of a full hit, set to every JSON type: the check refuses it, or the
    hit reads cleanly. A field the reader starts using without the check fails here."""
    # Positive control: the full hit passes and reads whole.
    datagov._check_results({"results": [_FULL]})
    full = datagov._normalize(_FULL)
    assert (full.id, full.title, full.description) == (
        "datagov:water-quality",
        "Water quality",
        "Samples",
    )
    assert [(f.name, f.url, f.mime) for f in full.files] == [
        ("Samples (CSV)", "https://example.gov/a.csv", "text/csv")
    ]
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(path, value)
            try:
                datagov._check_results({"results": [rec]})
            except _http.UpstreamEnvelopeError:
                continue
            try:
                datagov._normalize(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


def test_a_theme_given_as_a_skos_concept_is_read_by_its_label():
    """Live: center-for-tobacco-products-strategic-priority-i-public-education (2026-10-02)
    gives its only theme as a SKOS concept; the catalog's own top-level ``theme`` reads
    it as "Tobacco Products". The concept was dropped from subjects."""
    dcat = {
        "keyword": ["Tobacco Products/ Public Education"],
        "theme": [{"@type": "Concept", "prefLabel": "Tobacco Products"}],
    }
    assert datagov._subjects(dcat) == [
        "Tobacco Products/ Public Education",
        "Tobacco Products",
    ]
    # Positive control: string themes still read, after keywords, de-duplicated; a
    # concept without a label, and a term that is neither, add nothing.
    mixed = {
        "keyword": ["water", "water", 7],
        "theme": ["Environment", {"prefLabel": "water"}, {"@type": "Concept"}, ""],
    }
    assert datagov._subjects(mixed) == ["water", "Environment"]
