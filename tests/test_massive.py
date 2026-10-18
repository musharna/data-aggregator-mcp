"""The MassIVE adapter against verbatim answers of its dataset query and servlet (captured
2026-10-12), and live.

Round 3 (2026-10-11): the coral study MSV000101299 (T5 S15) names its marine heatwave
only in its description. OmicsDI's copy of MassIVE lags, and DataCite holds the DOI with
the title alone, so no source matched it to a heat-stress search.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import _mirror, datacite, massive, router
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, Link

_FIX = Path(__file__).parent / "fixtures"
LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
# The server's own label; its bodies are ISO-8859-1 bytes.
_LATIN1 = {"content-type": "application/json;charset=ISO-8859-1"}
S15 = "MSV000101299"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(massive._http.asyncio, "sleep", instant)


def _raw(name: str) -> bytes:
    return (_FIX / name).read_bytes()


def _rows(name: str) -> list[dict]:
    return json.loads(_raw(name).decode("iso-8859-1"))["row_data"]


# QueryDatasets, verbatim: one row per word ("count", pageSize=1), and the whole answer.
_COUNT = {w: _raw(f"massive_count_{w}.json") for w in ("pocillopora", "heatwave", "wave")}
_ALL = {w: _raw(f"massive_rows_{w}.json") for w in ("pocillopora", "heatwave")}
_EMPTY = _raw("massive_empty.json")
# The servlet's answer for an id it does not hold, sent as HTTP 404 text.
_NO_DATASET = _raw("massive_info_missing.txt").decode()


def _answer(rows: list[dict], total: int | None = None) -> bytes:
    """A QueryDatasets answer holding ``rows`` (verbatim rows, re-serialised)."""
    body = {"source": "MassIVE", "total_rows": str(len(rows) if total is None else total)}
    return json.dumps({**body, "row_data": rows}).encode("iso-8859-1")


def _client(sent: list[tuple[str, int, int]], answers: dict[str, object]):
    """A client answering QueryDatasets for a searched text from ``answers``: bytes are
    the answer to every page, a ``(count, rows)`` pair answers ``pageSize=1`` with the
    first and anything else with the second, and an unlisted text the empty answer. A
    servlet request is answered from ``answers["info:<acc>"]`` (bytes, or a str sent as
    404 text). A callable answers each page from its ``(pageSize, offset)``."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path.endswith("/MassiveServlet"):
            assert params["function"] == "massiveinformation"
            acc = params["massiveid"]
            sent.append((f"info:{acc}", 0, 0))
            body = answers.get(f"info:{acc}", _NO_DATASET)
            if isinstance(body, str):
                return httpx.Response(404, text=body)
            return httpx.Response(200, content=body)  # the servlet sends no content type
        assert request.url.path.endswith("/QueryDatasets"), request.url
        text = json.loads(params["query"])["full_search_input"]
        size, offset = int(params["pageSize"]), int(params["offset"])
        sent.append((text, size, offset))
        body = answers.get(text, _EMPTY)
        if callable(body):
            body = body(size, offset)
        if isinstance(body, tuple):
            body = body[0] if size == 1 else body[1]
        assert isinstance(body, bytes)
        return httpx.Response(200, content=body, headers=_LATIN1)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _words(*words: str) -> dict[str, object]:
    return {w: (_COUNT[w], _ALL.get(w)) for w in words}


async def _search(query: str, answers: dict[str, object] | None = None, **kw):
    sent: list[tuple[str, int, int]] = []
    answers = _words("pocillopora", "heatwave", "wave") if answers is None else answers
    async with _client(sent, answers) as c:
        return await massive.search(c, query, **kw), sent


@pytest.mark.asyncio
async def test_the_rarest_word_is_read_and_a_dataset_must_name_every_word():
    (total, recs), sent = await _search("pocillopora heatwave")
    # Each word counted with one row, then the rarest one's 3 rows read whole.
    assert sorted(sent[:2]) == [("heatwave", 1, 0), ("pocillopora", 1, 0)]
    assert sent[2:] == [("heatwave", 3, 0)]
    # Of the three heatwave datasets only S15 names Pocillopora (its species, keywords).
    assert (total, [r.id for r in recs]) == (1, [f"massive:{S15}"])
    # Positive control: either word alone keeps every dataset holding it.
    (total, _), _ = await _search("heatwave")
    assert total == 3
    (total, _), _ = await _search("pocillopora")
    assert total == 16


@pytest.mark.asyncio
async def test_a_word_may_be_named_by_the_keywords_alone():
    """S15's description says "metabolomic"; only its keywords say "Metabolomics", the
    word a T5 search sends."""
    s15 = next(r for r in _rows("massive_rows_heatwave.json") if r["dataset"] == S15)
    assert " metabolomics " not in massive._normalize_words(s15["title"] + " " + s15["description"])
    answers = {**_words("heatwave"), "metabolomics": (_answer([], total=2778), None)}
    (_, recs), sent = await _search("heatwave metabolomics", answers)
    assert sent[2:] == [("heatwave", 3, 0)]
    assert f"massive:{S15}" in [r.id for r in recs]


@pytest.mark.asyncio
async def test_a_word_held_only_inside_another_word_is_not_named():
    # The server matches substrings: S15 is among its 44 "wave" datasets for "heatwave".
    (total, recs), sent = await _search("pocillopora wave")
    assert sent[2:] == [("pocillopora", 16, 0)]  # 16 < 44: Pocillopora is the rarer
    assert (total, recs) == (0, [])
    # Positive control: the whole word is named.
    (total, recs), _ = await _search("pocillopora heatwave")
    assert [r.id for r in recs] == [f"massive:{S15}"]


@pytest.mark.asyncio
async def test_a_dataset_row_is_read_into_the_record():
    (_, recs), _ = await _search("pocillopora heatwave")
    r = recs[0]
    assert (r.source, r.kind, r.year) == ("massive", "study", 2026)
    assert r.title == "GNPS - Resilience of Tropical Eastern Pacific corals"
    assert r.description is not None and "natural marine heatwave" in r.description
    assert [c.name for c in r.creators] == ["Matthieu Leray"]
    assert r.organism == ["Pocillopora grandis", "Pocillopora verrucosa"]
    assert r.subjects == [
        "Metabolomics",
        "MSCollaboratory",
        "Pocillopora",
        "Coral",
        "DatasetType:Metabolomics",
    ]
    assert (r.accessions, r.doi) == ([S15], None)
    assert [(link.rel, link.target_id) for link in r.links] == [
        ("landing_page", f"https://massive.ucsd.edu/ProteoSAFe/dataset.jsp?accession={S15}")
    ]


@pytest.mark.asyncio
async def test_results_come_newest_first_and_page_by_offset():
    (total, recs), _ = await _search("pocillopora", size=3, offset=2)
    (_, every), _ = await _search("pocillopora", size=200)
    assert total == len(every) == 16
    assert every == sorted(every, key=lambda r: r.id, reverse=True)
    assert every[0].id == "massive:MSV000102666"
    assert [r.id for r in recs] == [r.id for r in every[2:5]]
    many = [
        dict(_rows("massive_rows_pocillopora.json")[0], dataset=f"MSV{i:09d}") for i in range(80)
    ]
    (total, recs), _ = await _search("pocillopora", {"pocillopora": _answer(many)}, size=200)
    assert (total, len(recs)) == (80, massive.MAX_SIZE)


@pytest.mark.asyncio
async def test_past_max_rows_the_newest_rows_of_the_rarest_word_are_read(monkeypatch):
    monkeypatch.setattr(massive, "MAX_ROWS", 10)
    rows = _rows("massive_rows_pocillopora.json")  # 16, oldest first, as the server sends

    def page(size: int, offset: int) -> bytes:
        return _answer(rows[offset : offset + size], total=16)

    sent: list[tuple[str, int, int]] = []
    answers = {"pocillopora": (page(1, 0), page(10, 6))}
    async with _client(sent, answers) as c:
        total, recs = await massive.search(c, "pocillopora", size=50)
    assert sent == [("pocillopora", 1, 0), ("pocillopora", 10, 6)]
    assert [r.id for r in recs] == [f"massive:{r['dataset']}" for r in reversed(rows[6:])]
    assert total == 10


@pytest.mark.asyncio
async def test_a_word_counted_for_one_search_is_not_counted_again_for_the_next():
    """An agent's searches rephrase one another: 235 of the 356 word counts of an
    83-search session repeated a word already counted (2026-10-17)."""
    (_, first), sent = await _search("pocillopora heatwave")
    assert sorted(sent[:2]) == [("heatwave", 1, 0), ("pocillopora", 1, 0)]
    (_, again), sent = await _search("heatwave pocillopora")
    assert sent == [("heatwave", 3, 0)]  # the rows alone: both counts were kept
    assert again == first
    # Positive control: a word not counted before is counted.
    _, sent = await _search("pocillopora wave")
    assert sent == [("wave", 1, 0), ("pocillopora", 16, 0)]


@pytest.mark.parametrize(
    ("max_rows", "reads", "newest"),
    [(5000, [(15, 0), (16, 0)], 16), (10, [(10, 5), (10, 6)], 10)],
)
@pytest.mark.asyncio
async def test_a_kept_count_that_has_grown_reads_the_rows_where_the_fresh_count_puts_them(
    monkeypatch, max_rows, reads, newest
):
    monkeypatch.setattr(massive, "MAX_ROWS", max_rows)
    rows = _rows("massive_rows_pocillopora.json")  # 16, oldest first, as the server sends

    def page(size: int, offset: int) -> bytes:
        return _answer(rows[offset : offset + size], total=16)

    massive._COUNTS.set("pocillopora", 15)  # kept before the newest dataset was deposited
    sent: list[tuple[str, int, int]] = []
    async with _client(sent, {"pocillopora": page}) as c:
        total, recs = await massive.search(c, "pocillopora", size=50)
    assert sent == [("pocillopora", *r) for r in reads]
    assert [r.id for r in recs] == [f"massive:{r['dataset']}" for r in reversed(rows)][:newest]
    assert total == newest
    # Positive control: the fresh count was kept, so the next search reads once.
    sent.clear()
    async with _client(sent, {"pocillopora": page}) as c:
        await massive.search(c, "pocillopora", size=50)
    assert sent == [("pocillopora", *reads[1])]


@pytest.mark.asyncio
async def test_a_kept_count_is_counted_again_after_ten_minutes(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(massive._COUNTS, "_now", lambda: clock[0])
    await _search("heatwave")
    clock[0] = 599.0
    assert massive._COUNTS.get("heatwave") == 3  # a search now would read the rows alone
    clock[0] = 600.0
    _, sent = await _search("heatwave")
    assert sent == [("heatwave", 1, 0), ("heatwave", 3, 0)]


@pytest.mark.asyncio
async def test_no_match_is_one_request_and_an_empty_page():
    (total, recs), sent = await _search("zzqxnonword")
    assert (total, recs, sent) == (0, [], [("zzqxnonword", 1, 0)])


@pytest.mark.asyncio
async def test_a_short_word_is_required_but_not_sent_unless_it_is_all_there_is():
    (total, recs), sent = await _search("pocillopora heatwave ms")
    assert {s[0] for s in sent} == {"pocillopora", "heatwave"}
    assert total == 1  # S15's description says "LC-MS/MS"
    massive._COUNTS.clear()  # so the words are counted again, not kept
    (total, recs), sent = await _search("pocillopora heatwave zq")
    assert {s[0] for s in sent} == {"pocillopora", "heatwave"}
    assert total == 0  # required, though never sent
    (_, recs), sent = await _search("lc", {"lc": (_COUNT["heatwave"], _ALL["heatwave"])})
    assert {s[0] for s in sent} == {"lc"}


@pytest.mark.asyncio
async def test_a_query_with_no_word_is_refused_without_a_request():
    with pytest.raises(ValueError, match="MassIVE search needs a word to look for; 'the OR of'"):
        await _search("the OR of")
    (_, recs), _ = await _search("the heatwave")
    assert len(recs) == 3


@pytest.mark.asyncio
async def test_a_body_in_iso_8859_1_is_read():
    """The server sends ISO-8859-1: MSV000080949's description holds "±" as byte 0xB1."""
    raw = _raw("massive_id_MSV000080949.latin1")
    assert b"\xb1" in raw
    (_, recs), _ = await _search("variability", {"variability": raw})
    assert recs and "±" in (recs[0].description or "")


def _row(**change) -> dict:
    """S15's verbatim row with ``change`` applied (``...`` deletes the key)."""
    row = copy.deepcopy(next(r for r in _rows("massive_rows_heatwave.json") if r["dataset"] == S15))
    for key, value in change.items():
        if value is ...:
            row.pop(key, None)
        else:
            row[key] = value
    return row


_MALFORMED = [
    pytest.param(b'{"detail": "Internal error"}', id="error-envelope"),
    pytest.param(b'{"total_rows": 3, "row_data": []}', id="count-not-a-string"),
    pytest.param(b'{"total_rows": "x", "row_data": []}', id="count-not-digits"),
    pytest.param(b'{"total_rows": "1", "row_data": {}}', id="rows-not-a-list"),
    pytest.param(_answer([_row(dataset="PXD000001")]), id="id-not-an-msv-accession"),
    pytest.param(_answer([_row(title=...)]), id="no-title"),
    pytest.param(_answer([_row(description=["x"])]), id="description-not-a-string"),
    pytest.param(_answer([_row(pis="Matthieu Leray")]), id="pis-not-a-list"),
    pytest.param(_answer([_row(pis=["Matthieu Leray"])]), id="pi-not-an-object"),
    pytest.param(b"<html>Service Unavailable</html>", id="html"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED)
async def test_an_answer_of_the_wrong_shape_fails_the_search(body):
    with pytest.raises(UpstreamUnavailableError, match="MassIVE search"):
        await _search("heatwave", {"heatwave": body})
    # Positive control: the same answer with the verbatim row is read.
    (_, recs), _ = await _search("heatwave", {"heatwave": _answer([_row()])})
    assert [r.id for r in recs] == [f"massive:{S15}"]


@pytest.mark.asyncio
async def test_a_row_without_its_optional_fields_reads_as_empty():
    bare = _row(description="null", keywords=..., species_resolved=..., create_time=..., pis=...)
    (_, recs), _ = await _search("resilience", {"resilience": _answer([bare])})
    r = recs[0]
    assert (r.description, r.subjects, r.organism, r.year, r.creators) == (None, [], [], None, [])
    px = _row(px="PXD012345")
    (_, recs), _ = await _search("resilience", {"resilience": _answer([px])})
    assert recs[0].accessions == [S15, "PXD012345"]


@pytest.mark.asyncio
async def test_resolve_adds_the_doi_and_ftp_location():
    sent: list[tuple[str, int, int]] = []
    answers = {
        S15: _raw("massive_id_MSV000101299.json"),
        f"info:{S15}": _raw("massive_info_MSV000101299.json"),
    }
    async with _client(sent, answers) as c:
        # Whitespace around the id and its case are not part of it.
        r = await massive.resolve(c, "massive: msv000101299 ")
    assert sorted(sent) == [(S15, 5, 0), (f"info:{S15}", 0, 0)]
    assert (r.id, r.doi, r.year) == (f"massive:{S15}", "10.25345/C5PG1J26B", 2026)
    assert ("data_access", f"ftp://massive-ftp.ucsd.edu/v12/{S15}/") in [
        (link.rel, link.target_id) for link in r.links
    ]
    # A dataset with no DOI: the servlet says "null".
    other = "MSV000080949"
    answers = {
        other: _raw("massive_id_MSV000080949.latin1"),
        f"info:{other}": _raw("massive_info_MSV000080949.json"),
    }
    async with _client(sent, answers) as c:
        assert (await massive.resolve(c, f"massive:{other}")).doi is None


@pytest.mark.asyncio
async def test_resolve_of_an_unknown_or_malformed_id_is_not_found():
    sent: list[tuple[str, int, int]] = []
    async with _client(sent, {}) as c:
        with pytest.raises(NotFoundError, match="MassIVE has no public dataset MSV999999999"):
            await massive.resolve(c, "massive:MSV999999999")
        for bad in ("massive:MSV00010129", "massive:PXD000001", "massive:MSV000101299/../x"):
            with pytest.raises(NotFoundError, match="malformed MassIVE dataset id"):
                await massive.resolve(c, bad)
    # The malformed ids never reached the network.
    assert sorted(sent) == [("MSV999999999", 5, 0), ("info:MSV999999999", 0, 0)]
    # A servlet answer for another dataset is not this one.
    answers = {
        S15: _raw("massive_id_MSV000101299.json"),
        f"info:{S15}": _raw("massive_info_MSV000080949.json"),
    }
    async with _client(sent, answers) as c:
        with pytest.raises(UpstreamUnavailableError, match="no MassIVE record of MSV000101299"):
            await massive.resolve(c, f"massive:{S15}")


# --- one deposit, three sources -----------------------------------------------------------


def _datacite_copy() -> DataResource:
    return datacite._normalize(json.loads(_raw("massive_datacite_c5pg1j26b.json"))["data"])


def _omicsdi_copy(acc: str) -> DataResource:
    """OmicsDI's record of a MassIVE dataset, as its search reads one."""
    return DataResource(
        id=f"omicsdi:massive:{acc}",
        source="omicsdi",
        kind="study",
        title="GNPS - Resilience of Tropical Eastern Pacific corals",
        accessions=[acc],
        links=[Link(rel="landing_page", target_id=f"https://www.omicsdi.org/x/{acc}")],
    )


@pytest.mark.asyncio
async def test_a_dataset_and_its_datacite_and_omicsdi_copies_are_one_deposit():
    (_, recs), _ = await _search("heatwave")
    native = next(r for r in recs if r.accessions == [S15])
    dc = _datacite_copy()
    assert dc.accessions == [S15] and dc.doi == "10.25345/c5pg1j26b"
    keys = {_mirror.deposit_key(r) for r in (native, dc, _omicsdi_copy(S15))}
    assert keys == {f"study:{S15}"}
    kept = _mirror.dedup_by_deposit([native, dc, _omicsdi_copy(S15)])
    assert [r.id for r in kept] == [f"omicsdi:massive:{S15}"]  # native fetch beats the rest
    assert [r.id for r in _mirror.dedup_by_deposit([native, dc])] == [dc.id]
    # Positive control: another dataset's copy stays its own hit.
    other = next(r for r in recs if r.accessions != [S15])
    assert len(_mirror.dedup_by_deposit([native, dc, other])) == 2


def test_only_a_massive_doi_reads_an_accession_from_its_title():
    item = json.loads(_raw("massive_datacite_c5pg1j26b.json"))["data"]
    assert datacite._normalize(item).accessions == [S15]
    other = copy.deepcopy(item)
    other["attributes"]["doi"] = "10.5281/zenodo.1"
    assert datacite._normalize(other).accessions == []
    untitled = copy.deepcopy(item)
    untitled["attributes"]["titles"][0]["title"] = "Resilience of corals (MSV000101299)"
    assert datacite._normalize(untitled).accessions == []


@pytest.mark.asyncio
async def test_the_router_names_massive_when_every_word_empties_it(monkeypatch):
    sent: list[tuple[str, int, int]] = []
    async with _client(sent, _words("pocillopora", "heatwave", "wave")) as c:
        monkeypatch.setattr(router, "_ADAPTERS", {"massive": massive})
        page = await router.search_page(c, query="pocillopora wave", sources=["massive"])
        assert page.errors["all_words"].startswith("massive matched nothing: it returns ")
        # Positive control: words a dataset names are no note.
        page = await router.search_page(c, query="pocillopora heatwave", sources=["massive"])
    assert "all_words" not in page.errors
    assert [r.id for r in page.results] == [f"massive:{S15}"]


# --- live ---------------------------------------------------------------------------------

_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "coral heatwave",
        # "metabolomics" is only in S15's keywords; its description says "metabolomic".
        "coral heat stress metabolomics",
        "pocillopora heat",
    ],
)
async def test_live_the_round_3_coral_heatwave_study_is_found(query):
    async with httpx.AsyncClient(timeout=120) as c:
        page = await router.search_page(c, query=query, sources=["massive"], size=50)
    assert "massive" not in page.errors, page.errors
    assert f"massive:{S15}" in {r.id for r in page.results}, [r.id for r in page.results]


@_live
@pytest.mark.asyncio
async def test_live_a_common_word_is_read_whole_in_its_own_charset():
    """ "metabolomics" is 2,778 datasets in one answer holding raw ISO-8859-1 bytes (0xB1
    at byte 223,752 on 2026-10-12); read as UTF-8 it was an outage."""
    async with httpx.AsyncClient(timeout=120) as c:
        total, recs = await massive.search(c, "metabolomics", size=5)
    assert total > 2000 and len(recs) == 5
    assert recs == sorted(recs, key=lambda r: r.id, reverse=True)


@_live
@pytest.mark.asyncio
async def test_live_resolve_reads_the_doi_datacite_holds():
    async with httpx.AsyncClient(timeout=120) as c:
        r = await massive.resolve(c, f"massive:{S15}")
        assert r.doi is not None and r.doi.casefold() == "10.25345/c5pg1j26b"
        with pytest.raises(NotFoundError):
            await massive.resolve(c, "massive:MSV999999999")


@_live
@pytest.mark.asyncio
async def test_live_the_dataset_and_its_datacite_doi_are_one_hit():
    query = "Resilience Tropical Eastern Pacific corals"
    async with httpx.AsyncClient(timeout=120) as c:
        page = await router.search_page(c, query=query, sources=["massive", "datacite"], size=50)
        # Positive control: DataCite alone returns its copy.
        alone = await router.search_page(c, query=query, sources=["datacite"], size=50)
    assert "datacite:10.25345/c5pg1j26b" in {r.id for r in alone.results}
    holding = [r.id for r in page.results if S15 in r.accessions]
    assert len(holding) == 1, holding


# --- what each request carries, and what a failure says ------------------------------------

_BAD = b'{"detail": "Internal error"}'


async def _failing(phase: str, call):
    """Run ``call`` on a client whose answer for ``phase`` is malformed and every other
    answer is verbatim: "count" (pageSize=1), "rows", "query" (resolve's dataset query)
    or "info" (the servlet). Returns the requests sent and the error raised, if any."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = request.url.params
        if request.url.path.endswith("/MassiveServlet"):
            body = _BAD if phase == "info" else _raw("massive_info_MSV000101299.json")
            return httpx.Response(200, content=body)
        text = json.loads(params["query"])["full_search_input"]
        if text == S15:
            body = _BAD if phase == "query" else _raw("massive_id_MSV000101299.json")
        elif params["pageSize"] == "1":
            body = _BAD if phase == "count" else _COUNT["heatwave"]
        else:
            body = _BAD if phase == "rows" else _ALL["heatwave"]
        return httpx.Response(200, content=body, headers=_LATIN1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        try:
            await call(c)
        except UpstreamUnavailableError as exc:
            return seen, str(exc)
    return seen, None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "service", "message"),
    [
        ("count", "MassIVE search", "no MassIVE dataset list in"),
        ("rows", "MassIVE search", "no MassIVE dataset list in"),
        ("query", "MassIVE resolve", "no MassIVE dataset list in"),
        ("info", "MassIVE resolve", "no MassIVE record of MSV000101299 in"),
    ],
)
async def test_a_malformed_answer_names_the_service_and_what_was_missing(phase, service, message):
    async def call(c):
        if phase in ("count", "rows"):
            await massive.search(c, "heatwave")
        else:
            await massive.resolve(c, f"massive:{S15}")

    seen, error = await _failing(phase, call)
    assert error is not None and error.startswith(
        f"[UpstreamUnavailableError] {service} returned an unparseable 200 body after "
    ), error
    # The message quotes the body, so its repr is double-quoted.
    assert f'UpstreamEnvelopeError("{message} {{' in error, error
    # Every request carries the source's own timeout.
    want = dict.fromkeys(("connect", "read", "write", "pool"), massive.DEFAULT_TIMEOUT)
    assert seen and all(request.extensions["timeout"] == want for request in seen)
    # Positive control: with every answer verbatim the same call succeeds.
    seen, error = await _failing("none", call)
    assert error is None and all(request.extensions["timeout"] == want for request in seen)


@pytest.mark.asyncio
async def test_a_word_of_three_letters_is_sent():
    _, sent = await _search("pocillopora rna")
    assert {s[0] for s in sent[:2]} == {"pocillopora", "rna"}


@pytest.mark.asyncio
async def test_a_blank_text_field_reads_as_absent():
    blank = _row(description="   ", keywords=" ", px="  ")
    (_, recs), _ = await _search("resilience", {"resilience": _answer([blank])})
    assert (recs[0].description, recs[0].subjects, recs[0].accessions) == (None, [], [S15])
    # Every reader tests the value for truth, so the contract is checked where it is made.
    assert massive._value(blank, "description") is None
    assert massive._value(blank, "title") == blank["title"]  # positive control


@pytest.mark.asyncio
async def test_resolve_needs_the_dataset_query_to_hold_it_even_when_the_servlet_answers():
    """A dataset the query does not list is not public, whatever the servlet says."""
    sent: list[tuple[str, int, int]] = []
    answers: dict[str, object] = {f"info:{S15}": _raw("massive_info_MSV000101299.json")}
    async with _client(sent, answers) as c:
        with pytest.raises(NotFoundError, match=f"MassIVE has no public dataset {S15}"):
            await massive.resolve(c, f"massive:{S15}")
    # Positive control: listed by the query too, it resolves.
    answers[S15] = _raw("massive_id_MSV000101299.json")
    async with _client(sent, answers) as c:
        assert (await massive.resolve(c, f"massive:{S15}")).id == f"massive:{S15}"
