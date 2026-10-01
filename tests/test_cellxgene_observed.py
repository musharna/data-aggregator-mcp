"""CELLxGENE behaviour the mutation run (nightly-guardrails, #88) showed no test observed.

128 mutants survived test_cellxgene.py. Its search fixtures matched on tissue only, so
nothing showed that the description, DOI, consortia or organism are searchable; record
fields were checked for presence (``any(... "geo" in ...)``); and the request each call
sends, its timeout, its retry budget and its error text were never pinned.

Shapes follow the live curation API (396 collections, 2026-09-30): every collection has
a UUID ``collection_id`` and a ``collection_url``; link types are upper-case
(``RAW_DATA``, ``OTHER``); 13 collections have ``published_year: null``.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, cellxgene
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry, Link

_A = "af893e86-8e9f-41f1-a474-ef05359b1fb7"
_B = "3a5dbf8a-9b3e-4309-b4c5-d8a024f83734"


def _ds(**fields: object) -> dict:
    return {"dataset_id": "ds", **fields}


def _term(label: str) -> dict:
    return {"label": label, "ontology_term_id": "X:1"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


class _Server:
    def __init__(self, answer: httpx.Response) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def _search(collections: list, query: str, **kw: int):
    async with _Server(httpx.Response(200, json=collections)).client() as c:
        return await cellxgene.search(c, query, **kw)


@pytest.mark.asyncio
async def test_search_request_url_header_and_timeout():
    srv = _Server(httpx.Response(200, json=[]))
    async with srv.client() as c:
        assert await cellxgene.search(c, "x") == (0, [])
    (req,) = srv.requests
    assert (req.method, str(req.url)) == ("GET", f"{cellxgene.API}/collections")
    assert req.headers["Accept"] == "application/json"
    assert req.extensions["timeout"]["read"] == cellxgene.DEFAULT_TIMEOUT == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "collection"),
    [
        ("respiratory", {"description": "Atlas of the respiratory tract"}),
        ("10.1038/xyz", {"doi": "10.1038/XYZ"}),
        ("hca", {"consortia": ["HCA"]}),
        ("musculus", {"datasets": [_ds(organism=[_term("Mus musculus")])]}),
        ("fibrosis", {"datasets": [_ds(disease=[_term("pulmonary fibrosis")])]}),
        ("smart-seq2", {"datasets": [_ds(assay=[_term("Smart-seq2")])]}),
    ],
)
async def test_search_matches_every_searchable_field(query, collection):
    other = {"collection_id": _B, "name": "Unrelated"}
    total, recs = await _search([{"collection_id": _A, **collection}, other], query)
    assert (total, [r.id for r in recs]) == (1, [f"cellxgene:{_A}"])


@pytest.mark.asyncio
async def test_search_text_does_not_join_fields_into_new_words():
    # "lung atlas" spans two fields; the joined blob separates them with a space only.
    col = {"collection_id": _A, "name": "Lung", "description": "Atlas", "doi": None}
    assert (await _search([col], "lung atlas"))[0] == 1
    assert (await _search([col], "xx"))[0] == 0
    assert (await _search([col], "lungatlas"))[0] == 0


@pytest.mark.asyncio
async def test_search_drops_collections_without_an_id_and_windows_by_size():
    cols = [{"name": "No id"}, "junk"] + [
        {"collection_id": f"{i:08d}-0000-4000-8000-000000000000", "name": "Hit"} for i in range(3)
    ]
    total, recs = await _search(cols, "")
    assert total == 3 and len(recs) == 3
    total, recs = await _search(cols, "hit", size=1, offset=2)
    assert (total, [r.id for r in recs]) == (3, ["cellxgene:00000002-0000-4000-8000-000000000000"])
    assert await _search(cols, "hit", size=0) == (3, [])


def test_normalize_record_whole():
    col = {
        "collection_id": _A,
        "collection_url": "https://cellxgene.cziscience.com/collections/canonical-url",
        "name": "Lung atlas",
        "description": "An atlas.",
        "doi": "10.1/x",
        "published_at": "2021-05-06T16:41:21+00:00",
        "revised_at": "2025-10-24T21:07:43+00:00",
        "publisher_metadata": {"published_year": 2020},  # wins over published_at
        "links": [
            {"link_type": "RAW_DATA", "link_url": "https://geo/1"},
            {"link_type": None, "link_url": "https://other/2"},
            {"link_type": "OTHER", "link_url": None},
        ],
        "datasets": [
            _ds(organism=[_term("Homo sapiens")], tissue=[_term("lung"), "bad"]),
            _ds(organism=[_term("Homo sapiens"), _term("Mus musculus")], tissue=[_term("lung")]),
        ],
    }
    r = cellxgene._normalize(col)
    assert r.description == "An atlas." and r.access == "open" and r.year == 2020
    assert r.last_updated == "2025-10-24T21:07:43+00:00"
    assert r.organism == ["Homo sapiens", "Mus musculus"]
    assert r.subjects == ["lung"]
    assert r.links == [
        Link(
            rel="landing_page",
            target_id="https://cellxgene.cziscience.com/collections/canonical-url",
        ),
        Link(rel="raw_data", target_id="https://geo/1"),
        Link(rel="related", target_id="https://other/2"),
    ]


def test_normalize_fallbacks():
    r = cellxgene._normalize(
        {"collection_id": _A, "published_at": "2019-01-02T00:00:00+00:00", "publisher_metadata": {}}
    )
    assert r.title == _A
    assert r.year == 2019  # published_year null (13/396 live): read it from published_at
    assert r.last_updated == "2019-01-02T00:00:00+00:00"
    assert r.links == [
        Link(rel="landing_page", target_id=f"https://cellxgene.cziscience.com/collections/{_A}")
    ]
    bare = cellxgene._normalize({"collection_id": _A, "published_at": "unknown"})
    assert (bare.year, bare.last_updated, bare.description) == (None, "unknown", None)
    assert cellxgene._normalize({"collection_id": _A}).last_updated is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a" * 500, "a" * 500),
        ("a" * 497 + "   " + "b", "a" * 497 + "…"),
        ("  lead" + "c" * 600, "  lead" + "c" * 494 + "…"),
    ],
)
def test_description_is_cut_at_500_characters(text, expected):
    assert cellxgene._truncate(text) == expected


def test_file_manifest_names_skips_and_cap():
    col = {
        "datasets": [
            {
                "dataset_id": "ds-1",
                "assets": [
                    {"filetype": "H5AD", "url": None},
                    {"filetype": "H5AD", "filesize": 5, "url": "https://d/a/0f3c.h5ad"},
                    {"filesize": 6, "url": "https://d/a/b/raw.bin?sig=1"},
                ],
            },
            {"title": "Named", "assets": [{"filetype": "RDS", "url": "https://d/n.rds"}]},
            {"assets": [{"filetype": "H5AD", "url": "https://d/x/anon.h5ad"}]},
        ]
    }
    assert cellxgene._file_manifest(col) == [
        FileEntry(name="ds-1.h5ad", size=5, url="https://d/a/0f3c.h5ad", source="cellxgene"),
        # no file type: the URL path's last segment, without the query string
        FileEntry(name="raw.bin", size=6, url="https://d/a/b/raw.bin?sig=1", source="cellxgene"),
        FileEntry(name="Named.rds", url="https://d/n.rds", source="cellxgene"),
        # no title and no dataset id: the URL path's last segment
        FileEntry(name="anon.h5ad", url="https://d/x/anon.h5ad", source="cellxgene"),
    ]
    many = {"datasets": [{"title": "t", "assets": [{"url": f"https://d/{i}"} for i in range(201)]}]}
    files = cellxgene._file_manifest(many)
    assert len(files) == cellxgene.MANIFEST_CAP == 200 and files[-1].url == "https://d/199"


@pytest.mark.asyncio
async def test_resolve_request_and_not_found_text():
    srv = _Server(httpx.Response(404, json={"detail": "Resource not found."}))
    async with srv.client() as c:
        with pytest.raises(NotFoundError) as missing:
            await cellxgene.resolve(c, f"cellxgene:{_A}")
        with pytest.raises(NotFoundError) as malformed:
            await cellxgene.resolve(c, "cellxgene:nope")
    assert str(missing.value) == f"[NotFoundError] CELLxGENE has no collection {_A}"
    assert str(malformed.value) == "[NotFoundError] malformed CELLxGENE id 'cellxgene:nope'"
    (req,) = srv.requests
    assert (req.method, str(req.url)) == ("GET", f"{cellxgene.API}/collections/{_A}")
    assert req.headers["Accept"] == "application/json"
    assert req.extensions["timeout"]["read"] == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "service"),
    [
        (lambda c: cellxgene.search(c, "x"), "CELLxGENE search"),
        (lambda c: cellxgene.resolve(c, f"cellxgene:{_A}"), "CELLxGENE resolve"),
    ],
)
async def test_outage_is_retried_twice_and_names_the_call(call, service):
    srv = _Server(httpx.Response(503, text="busy"))
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await call(c)
    assert (
        str(err.value)
        == f"[UpstreamUnavailableError] {service} exhausted 2 retries (last HTTP 503)"
    )
    assert len(srv.requests) == cellxgene.MAX_RETRIES
