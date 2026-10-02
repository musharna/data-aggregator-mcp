"""How the Hugging Face adapter reads what the Hub answers, from live shapes.

``_SROIE`` is a verbatim search record (``/api/datasets?search=SROIE_2019&full=true``,
2026-10-02). Its card says ``license: []`` and it has no ``license:`` tag. In 1,500 live
search records the card licence was a string 605 times and a list 69 times.
"""

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, huggingface
from data_aggregator_mcp.errors import UpstreamUnavailableError

_SROIE = {
    "_id": "630a84ffe81e1dea2cef3404",
    "id": "priyank-m/SROIE_2019_text_recognition",
    "author": "priyank-m",
    "cardData": {
        "annotations_creators": [],
        "language": ["en"],
        "language_creators": [],
        "license": [],
        "multilinguality": ["monolingual"],
        "pretty_name": "SROIE_2019_text_recognition",
        "size_categories": ["10K<n<100K"],
        "source_datasets": [],
        "tags": ["text-recognition", "recognition"],
        "task_categories": ["image-to-text"],
        "task_ids": ["image-captioning"],
    },
    "disabled": False,
    "gated": False,
    "lastModified": "2022-08-27T21:38:24.000Z",
    "likes": 14,
    "trendingScore": 0,
    "private": False,
    "sha": "04f6537e418eeb88863d617eb27817cc496522d7",
    "downloads": 320,
    "tags": [
        "task_categories:image-to-text",
        "task_ids:image-captioning",
        "multilinguality:monolingual",
        "language:en",
        "size_categories:10K<n<100K",
        "format:imagefolder",
        "modality:image",
        "modality:text",
        "library:datasets",
        "library:mlcroissant",
        "region:us",
        "text-recognition",
        "recognition",
    ],
    "createdAt": "2022-08-27T20:56:31.000Z",
    "key": "",
}

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(body, sent: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        if request.url.host == "datasets-server.huggingface.co":
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_card_listing_no_licence_is_a_record_without_one():
    """``license: []`` reached ``DataResource.license`` as a list: a ValidationError that
    lost the whole search page and the resolve. Positive control: the rest of the record
    is read."""
    async with _client([_SROIE]) as c:
        total, recs = await huggingface.search(c, "SROIE_2019")
    assert total == 1 and recs[0].license is None
    assert recs[0].id == "hf:priyank-m/SROIE_2019_text_recognition"
    assert recs[0].subjects == ["text-recognition", "recognition"]
    async with _client({**_SROIE, "siblings": [{"rfilename": "README.md"}]}) as c:
        r = await huggingface.resolve(c, "hf:priyank-m/SROIE_2019_text_recognition")
    assert r.license is None and [f.name for f in r.files] == ["README.md"]


@pytest.mark.parametrize(
    ("card_licence", "expected"),
    [
        ("mit", "mit"),
        (["apache-2.0", "mit"], "apache-2.0"),
        (["cc-by-4.0"], "cc-by-4.0"),
        ([], None),
        ("", None),
        (3, None),
        ([3], None),
    ],
)
def test_the_card_licence_is_read_when_no_tag_names_one(card_licence, expected):
    """A string or the first of a list; anything else is no licence. Positive control: a
    ``license:`` tag still wins over the card."""
    rec = {**_SROIE, "cardData": {"license": card_licence}}
    assert huggingface._normalize(rec).license == expected
    tagged = {**rec, "tags": ["license:gpl-3.0", *_SROIE["tags"]]}
    assert huggingface._normalize(tagged).license == "gpl-3.0"


def test_a_licence_tag_keeps_everything_after_the_first_colon():
    rec = {**_SROIE, "tags": ["license:other:custom", "biology"]}
    assert huggingface._normalize(rec).license == "other:custom"


def _broken(field: str, value: object) -> dict:
    rec = copy.deepcopy(_SROIE)
    rec[field] = value
    return rec


_MALFORMED = [
    pytest.param(_broken("id", None), id="id-null"),
    pytest.param({k: v for k, v in _SROIE.items() if k != "id"}, id="id-absent"),
    pytest.param(_broken("id", 7), id="id-int"),
    pytest.param(_broken("id", ""), id="id-empty"),
    pytest.param(_broken("id", "owner/a..b"), id="id-dotdot"),
    pytest.param(_broken("id", "owner/a b"), id="id-space"),
    pytest.param(_broken("author", ["priyank-m"]), id="author-list"),
    pytest.param(_broken("createdAt", 2022), id="createdAt-int"),
    pytest.param(_broken("lastModified", 1), id="lastModified-int"),
    pytest.param(_broken("cardData", "license: mit"), id="cardData-str"),
    pytest.param(_broken("downloads", "320"), id="downloads-str"),
    pytest.param(_broken("likes", "14"), id="likes-str"),
    pytest.param(_broken("tags", "biology"), id="tags-str"),
    pytest.param(_broken("tags", ["biology", 3]), id="tags-int-item"),
    pytest.param(_broken("siblings", {"rfilename": "a"}), id="siblings-dict"),
    pytest.param(_broken("siblings", ["a.csv"]), id="siblings-str-item"),
    pytest.param(_broken("siblings", [{"rfilename": 1}]), id="siblings-int-name"),
    pytest.param(_broken("siblings", [{"size": 1}]), id="siblings-no-name"),
    pytest.param("priyank-m/SROIE_2019_text_recognition", id="not-a-dict"),
]


@pytest.mark.parametrize("bad", _MALFORMED)
def test_a_field_normalize_reads_at_the_wrong_type_is_not_a_dataset(bad):
    """Each escaped as a bare TypeError/AttributeError/ValidationError, or (no id) became
    a record ``hf:``. Positive control: the live record, and every optional field null
    or absent, are datasets."""
    assert not huggingface._is_dataset(bad)
    assert huggingface._is_dataset(_SROIE)
    assert huggingface._is_dataset({"id": "squad"})
    nulls = dict.fromkeys(huggingface._FIELDS)
    assert huggingface._is_dataset({**_SROIE, **nulls})
    assert huggingface._is_dataset({**_SROIE, "siblings": [{"rfilename": "a.csv"}]})


@pytest.mark.asyncio
async def test_a_malformed_search_answer_is_upstream_trouble_after_two_tries():
    sent: list[httpx.Request] = []
    async with _client([_SROIE, _broken("tags", "biology")], sent) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] HuggingFace search returned an unparseable 200 body after 2 tries: "
            r"UpstreamEnvelopeError\(\"no HuggingFace dataset list in \[\{'_id': ",
        ):
            await huggingface.search(c, "SROIE_2019")
    assert len(sent) == 2
    async with _client([_SROIE]) as c:  # positive control
        assert (await huggingface.search(c, "SROIE_2019"))[0] == 1


@pytest.mark.asyncio
async def test_a_malformed_resolve_answer_is_upstream_trouble_after_two_tries():
    sent: list[httpx.Request] = []
    async with _client(_broken("siblings", ["README.md"]), sent) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] HuggingFace resolve returned an unparseable 200 body after 2 tries: "
            r"UpstreamEnvelopeError\(\"no HuggingFace dataset in \{'_id': ",
        ):
            await huggingface.resolve(c, "hf:priyank-m/SROIE_2019_text_recognition")
    assert len(sent) == 2
    async with _client(_SROIE) as c:  # positive control
        r = await huggingface.resolve(c, "hf:priyank-m/SROIE_2019_text_recognition")
    assert r.title == "priyank-m/SROIE_2019_text_recognition"


@_live_only
@pytest.mark.asyncio
async def test_live_a_card_listing_no_licence_is_searched_and_resolved():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        _total, recs = await huggingface.search(c, "SROIE_2019")
        hit = [r for r in recs if r.id == "hf:priyank-m/SROIE_2019_text_recognition"]
        assert hit and hit[0].license is None
        assert [r for r in recs if r.license], "other hits still carry their licence"
        r = await huggingface.resolve(c, "hf:priyank-m/SROIE_2019_text_recognition")
        assert r.license is None and r.files


@_live_only
@pytest.mark.asyncio
async def test_live_every_search_record_is_a_dataset():
    """The check rejects no live record (1,533 of 1,533 on 2026-10-02)."""
    async with httpx.AsyncClient(timeout=60) as c:
        for query in ("dna", "text", "images"):
            body = await _http.request_json(
                c,
                "GET",
                huggingface.API,
                service="HuggingFace search",
                params={"search": query, "limit": huggingface.MAX_SIZE, "full": "true"},
                expect=list,
            )
            assert len(body) == huggingface.MAX_SIZE
            assert [d["id"] for d in body if not huggingface._is_dataset(d)] == []


@_live_only
@pytest.mark.asyncio
async def test_live_a_file_url_with_a_space_serves_the_file():
    """The escaped file URL (``Approved IP Law.pdf``) is served, as is a plain one."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await huggingface.resolve(c, "hf:omar87/pdf-laws")
        urls = {f.name: f.url for f in r.files if f.source != "hf-datasets-server"}
        assert "%20" in urls["Approved IP Law.pdf"]
        for name in ("Approved IP Law.pdf", "CabinetDecision_47_2022_pdf.pdf"):
            head = await c.head(urls[name])
            assert head.status_code == 200, (name, head.status_code)


_FULL = {**_SROIE, "siblings": [{"rfilename": "README.md"}, {"rfilename": "data/a b.csv"}]}
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
    huggingface._check_dataset(_FULL)
    r = huggingface._normalize(_FULL)
    assert (r.year, r.metrics.downloads, r.metrics.likes) == (2022, 320, 14)
    assert [f.name for f in r.files] == ["README.md", "data/a b.csv"]
    escaped = []
    for path in _paths(_FULL):
        if not path:
            continue
        for value in _WRONG:
            rec = _with(path, value)
            try:
                huggingface._check_dataset(rec)
            except _http.UpstreamEnvelopeError:
                continue
            try:
                huggingface._normalize(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []
