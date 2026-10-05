"""What the OmicsDI adapter sends and how it reads what comes back, pinned exactly.

Shapes are the live captures in ``test_omicsdi_answers.py``.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, omicsdi
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import SEARCH_DESC_LIMIT, Creator, DataResource, FileEntry, Link
from tests.test_omicsdi_answers import HIT, MSV000081764, MTBLS806, PXD002724


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _client(sent: list[httpx.Request], *responses: httpx.Response) -> httpx.AsyncClient:
    answers = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call(r: httpx.Request) -> tuple[str, str, str | None]:
    return r.method, str(r.url), r.headers.get("accept")


# --- search -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "size"),
    [({}, "10"), ({"size": 3}, "3"), ({"size": 50}, "50"), ({"size": 51}, "50")],
)
async def test_search_sends_exactly_one_request(kwargs, size):
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json={"count": 0, "datasets": []})) as c:
        assert await omicsdi.search(c, "lung cancer", **kwargs) == (0, [])
    query = (
        "%28%28lung+OR+lungs%29+%28cancer+OR+cancers%29%29+AND+repository%3A%28%22pride%22+OR+%22MassIVE%22+OR+%22jPOST%22"
        "+OR+%22iProX%22+OR+%22PeptideAtlas%22+OR+%22PanoramaPublic%22+OR+%22MetaboLights%22"
        "+OR+%22MetabolomicsWorkbench%22+OR+%22GNPS%22%29"
    )
    assert [_call(r) for r in sent] == [
        (
            "GET",
            f"https://www.omicsdi.org/ws/dataset/search?query={query}&size={size}",
            "application/json",
        )
    ]


@pytest.mark.asyncio
async def test_search_past_the_first_page_asks_for_that_page():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json={"count": 51, "datasets": [HIT]})) as c:
        assert (await omicsdi.search(c, "x", offset=50))[0] == 51
        # positive control: offset 0 is the first page, sent with no start
        assert (await omicsdi.search(c, "x", offset=0))[0] == 51
    assert [(r.url.params.get("start"), r.url.params["size"]) for r in sent] == [
        ("50", "10"),
        (None, "10"),
    ]


@pytest.mark.asyncio
async def test_search_reads_each_hit_whole():
    long = "d" * 2000
    hits = [
        {**HIT, "description": long},
        {"id": "ST001684", "source": "metabolomics_workbench", "title": None},
        {"id": "MSV1", "source": "gnps", "title": "g", "description": ""},
        {"id": "MSV2", "source": "massive", "title": "m"},
        {"id": "PAe1", "source": "peptide_atlas", "title": "p"},
        {"id": "MTBLS1", "source": "metabolights_dataset", "title": "l"},
        {"id": "RPXD049832", "source": "jpost", "title": "j"},
    ]
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json={"count": 9, "datasets": hits})) as c:
        total, recs = await omicsdi.search(c, "x")
    assert total == 9  # OmicsDI's count of matches, not the page's length
    assert [r.id for r in recs] == [
        "omicsdi:pride:PXD006873",
        "omicsdi:metabolomics_workbench:ST001684",
        "omicsdi:gnps:MSV1",
        "omicsdi:massive:MSV2",
        "omicsdi:peptide_atlas:PAe1",
        "omicsdi:metabolights_dataset:MTBLS1",
        "omicsdi:jpost:RPXD049832",
    ]
    assert recs[0] == DataResource(
        id="omicsdi:pride:PXD006873",
        source="omicsdi",
        kind="study",
        title=HIT["title"],
        description=long[:SEARCH_DESC_LIMIT],
        links=[
            Link(rel="landing_page", target_id="https://www.omicsdi.org/dataset/pride/PXD006873")
        ],
    )
    assert len(long) > SEARCH_DESC_LIMIT  # so the description was compacted
    assert (recs[1].title, recs[1].description, recs[2].description) == ("", None, None)


@pytest.mark.asyncio
async def test_search_retries_once_then_names_the_call():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] OmicsDI search exhausted 2 retries \(last HTTP 503\)$",
        ):
            await omicsdi.search(c, "x")
    assert len(sent) == 2
    # positive control: one 503 then an answer is an answer
    sent.clear()
    async with _client(
        sent, httpx.Response(503), httpx.Response(200, json={"count": 1, "datasets": [HIT]})
    ) as c:
        assert (await omicsdi.search(c, "x"))[0] == 1
    assert len(sent) == 2


# --- resolve ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_sends_exactly_one_request_and_reads_the_record_whole():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=MSV000081764)) as c:
        r = await omicsdi.resolve(c, "omicsdi:massive:MSV000081764")
    assert [_call(x) for x in sent] == [
        ("GET", "https://www.omicsdi.org/ws/dataset/massive/MSV000081764", "application/json")
    ]
    assert r == DataResource(
        id="omicsdi:massive:MSV000081764",
        source="omicsdi",
        kind="study",
        title="A proteomic map of the human aqueous humor",
        description=MSV000081764["description"],
        creators=[Creator(name="Akhilesh Pandey")],
        organism=["Homo Sapiens (ncbitaxon:9606)"],
        links=[
            Link(
                rel="landing_page",
                target_id="https://www.omicsdi.org/dataset/massive/MSV000081764",
            )
        ],
    )


@pytest.mark.asyncio
async def test_resolve_an_unknown_accession_is_not_found():
    sent: list[httpx.Request] = []
    gone = httpx.Response(404, json={"status": 404, "error": "Not Found"})
    async with _client(sent, gone) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] OmicsDI has no pride/PXD999999999$"
        ):
            await omicsdi.resolve(c, "omicsdi:pride:PXD999999999")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_resolve_retries_once_then_names_the_call():
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(502)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] OmicsDI resolve exhausted 2 retries \(last HTTP 502\)$",
        ):
            await omicsdi.resolve(c, "omicsdi:massive:MSV000081764")
    assert len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rid", ["omicsdi:onlytwo", "omicsdi", "omicsdi:pride:PXD1:x"])
async def test_resolve_names_a_malformed_id(rid):
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=MSV000081764)) as c:
        with pytest.raises(
            NotFoundError, match=rf"^\[NotFoundError\] malformed OmicsDI id '{rid}'$"
        ):
            await omicsdi.resolve(c, rid)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "body", "routed"),
    [
        ("pride", PXD002724, "pride"),
        ("metabolights_dataset", MTBLS806, "metabolights"),
        ("massive", MSV000081764, None),
        ("gnps", MSV000081764, None),
    ],
)
async def test_resolve_lists_files_from_the_repository_that_holds_them(
    monkeypatch, source, body, routed
):
    acc = body["accession"]
    calls = []

    def lister(name):
        async def files(client, accession):
            calls.append((name, client, accession))
            return [FileEntry(name=f"{name}.raw", url=f"https://x/{name}.raw", source=name)]

        return files

    monkeypatch.setattr(omicsdi.pride, "files", lister("pride"))
    monkeypatch.setattr(omicsdi.metabolights, "files", lister("metabolights"))
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=body)) as c:
        r = await omicsdi.resolve(c, f"omicsdi:{source}:{acc}")
    if routed is None:
        assert calls == [] and r.files == []
    else:
        assert calls == [(routed, c, acc)]
        assert [f.name for f in r.files] == [f"{routed}.raw"]
    assert r.id == f"omicsdi:{source}:{acc}" and r.title == body["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("additional", "creators", "organism"),
    [
        # the first key with a usable value wins; blanks are skipped, names stripped and deduped
        (
            {"submitter": [" A ", "", "A", "B"], "submitter_name": ["C"], "species": ["S"]},
            ["A", "B"],
            ["S"],
        ),
        ({"submitter": ["  "], "submitter_name": ["C"], "organism": ["O"]}, ["C"], ["O"]),
        ({"submitter": [], "submitter_name": ["C", "D"]}, ["C", "D"], []),
        ({"species": ["S1", "S2"], "organism": ["O"]}, [], ["S1", "S2"]),
        ({"author": ["Paper Author"]}, [], []),
        ({}, [], []),
    ],
)
async def test_creators_and_organism_come_from_one_key(additional, creators, organism):
    body = copy.deepcopy(MSV000081764)
    body["additional"] = additional
    sent: list[httpx.Request] = []
    async with _client(sent, httpx.Response(200, json=body)) as c:
        r = await omicsdi.resolve(c, "omicsdi:massive:MSV000081764")
    assert [x.name for x in r.creators] == creators
    assert r.organism == organism
