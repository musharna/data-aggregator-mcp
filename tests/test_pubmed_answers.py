"""How the PubMed adapter reads what NCBI answers, including the answers that are failures.

Live shapes (2026-10-02, no API key):

- esummary ``id=34320281,1``: both records. E-utilities read ``id`` as a LIST, so
  ``pubmed:34320281,1`` resolved PMID 34320281 carrying PMID 1's 11 SRA links and both
  abstracts.
- esummary ``id=abc``: ``{"error": "Invalid uid abc at position= 0", "result": {"uids": []}}``
  inside a 200, retried and reported as an outage.
- esearch ``retstart=9999`` and above: ``esearchresult.ERROR`` "Search Backend failed:
  Exception:<raw newline>'retstart' cannot be larger than 9998. For PubMed, ESearch can only
  retrieve the first 9,999 records ...". The raw newline makes the body invalid JSON, so it
  was retried and reported as an outage on every page past the window.
- elink pubmed→sra for PMID 42544407: 7,498 uids. One esummary of them all is a URL httpx
  refuses (``InvalidURL``); 2,000 is HTTP 414; more than 500 is ``"Too many UIDs in
  request. Maximum number of UIDs is 500 for JSON format output."`` inside a 200.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _eutils, _http, omics, pubmed
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError, ValidationError
from data_aggregator_mcp.models import Creator, DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# esummary db=pubmed version=2.0, PMID 42802988, verbatim (live 2026-10-02).
LIVE_DOC: dict[str, Any] = {
    "uid": "42802988",
    "pubdate": "2026",
    "epubdate": "",
    "source": "Biomed Res Int",
    "authors": [
        {"name": "Imani M", "authtype": "Author", "clusterid": ""},
        {"name": "Lotfi M", "authtype": "Author", "clusterid": ""},
        {"name": "Shahcheraghi SH", "authtype": "Author", "clusterid": ""},
    ],
    "lastauthor": "Shahcheraghi SH",
    "title": (
        "Propofol and Glioblastoma Biology: A Narrative Review of Perioperative Clinical "
        "Evidence and PI3K/AKT-Wnt-miRNA, GABA-A, and Immune-Checkpoint Pathways."
    ),
    "sorttitle": (
        "propofol and glioblastoma biology a narrative review of perioperative clinical "
        "evidence and pi3k akt wnt mirna gaba a and immune checkpoint pathways"
    ),
    "volume": "2026",
    "issue": "1",
    "pages": "e4808235",
    "lang": ["eng"],
    "nlmuniqueid": "101600173",
    "issn": "2314-6133",
    "essn": "2314-6141",
    "pubtype": ["Journal Article", "Review"],
    "recordstatus": "PubMed - indexed for MEDLINE",
    "pubstatus": "4",
    "articleids": [
        {"idtype": "pubmed", "idtypen": 1, "value": "42802988"},
        {"idtype": "pmc", "idtypen": 8, "value": "PMC13617464"},
        {"idtype": "pmcid", "idtypen": 5, "value": "pmc-id: PMC13617464;"},
        {"idtype": "doi", "idtypen": 3, "value": "10.1155/bmri/4808235"},
    ],
    "history": [
        {"pubstatus": "revised", "date": "2026/07/05 00:00"},
        {"pubstatus": "received", "date": "2025/12/18 00:00"},
        {"pubstatus": "accepted", "date": "2026/08/26 00:00"},
        {"pubstatus": "medline", "date": "2026/09/28 11:40"},
        {"pubstatus": "pubmed", "date": "2026/09/28 05:33"},
        {"pubstatus": "entrez", "date": "2026/09/28 04:53"},
        {"pubstatus": "pmc-release", "date": "2026/09/28 00:00"},
    ],
    "references": [],
    "attributes": ["Has Abstract"],
    "pmcrefcount": 56,
    "fulljournalname": "BioMed research international",
    "elocationid": "doi: 10.1155/bmri/4808235",
    "doctype": "citation",
    "srccontriblist": [],
    "booktitle": "",
    "medium": "",
    "edition": "",
    "publisherlocation": "",
    "publishername": "",
    "srcdate": "",
    "reportnumber": "",
    "availablefromurl": "",
    "locationlabel": "",
    "doccontriblist": [],
    "docdate": "",
    "bookname": "",
    "chapter": "",
    "sortpubdate": "2026/01/01 00:00",
    "sortfirstauthor": "Imani M",
    "vernaculartitle": "",
}
EXPECTED = DataResource(
    id="pubmed:42802988",
    source="pubmed",
    kind="publication",
    title=LIVE_DOC["title"],
    creators=[Creator(name="Imani M"), Creator(name="Lotfi M"), Creator(name="Shahcheraghi SH")],
    year=2026,
    doi="10.1155/bmri/4808235",
    identifiers={"pmid": "42802988", "doi": "10.1155/bmri/4808235", "pmcid": "PMC13617464"},
)
# esearch db=pubmed retstart=9999 (live 2026-10-02), the bytes verbatim: a raw newline
# inside the ERROR string, so not valid JSON.
ESEARCH_PAST_WINDOW = (
    b'{"header":{"type":"esearch","version":"0.3"},"esearchresult":{"ERROR":"Search Backend '
    b"failed: Exception:\n'retstart' cannot be larger than 9998. For PubMed, ESearch can "
    b"only retrieve the first 9,999 records matching the query. To obtain more than 9,999 "
    b"PubMed records, consider using EDirect that contains additional logic to batch PubMed "
    b"search results automatically so that an arbitrary number can be retrieved. For "
    b'details see https://www.ncbi.nlm.nih.gov/books/NBK25499/"}}\n'
)
TOO_MANY_UIDS = {
    "header": {"type": "esummary", "version": "0.3"},
    "error": "Too many UIDs in request. Maximum number of UIDs is 500 for JSON format output.",
}


@pytest.fixture(autouse=True)
def _offline_ncbi(request, monkeypatch) -> None:
    """Offline tests: no API key in the URLs and no real backoff. The live tests keep the
    real waits, which the NCBI rate limiter needs."""
    if "live" in request.node.name:
        return

    async def _no_sleep(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)


def _endpoint(request: httpx.Request) -> str:
    return request.url.path.rsplit("/", 1)[-1]


@pytest.mark.parametrize(
    "pmid", ["34320281,1", "abc", "../../evil?injected=1", " 34320281", "1:2", ""]
)
async def test_a_pmid_that_is_not_a_number_is_not_found_before_the_network(pmid: str) -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"header": {}, "result": {"uids": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        with pytest.raises(
            NotFoundError,
            match=rf"^\[NotFoundError\] no pubmed record for {re.escape(repr(pmid))}: a PMID is a decimal number$",
        ):
            await pubmed.resolve(client, f"pubmed:{pmid}")
        assert sent == []
        # Positive control: a well-formed PMID is asked of NCBI (which has no such record).
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] no pubmed record for '34320281'$"
        ):
            await pubmed.resolve(client, "pubmed:34320281")
    assert [(_endpoint(r), r.url.params["id"]) for r in sent] == [("esummary.fcgi", "34320281")]


async def test_a_search_past_pubmeds_window_is_refused_not_an_outage() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if _endpoint(request) == "esearch.fcgi":
            if int(request.url.params.get("retstart", "0")) > 9998:
                return httpx.Response(200, content=ESEARCH_PAST_WINDOW)
            return httpx.Response(
                200, json={"esearchresult": {"count": "5714011", "idlist": ["42802988"]}}
            )
        return httpx.Response(200, json={"result": {"uids": ["42802988"], "42802988": LIVE_DOC}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        with pytest.raises(
            ValidationError,
            match=(
                r"^\[ValidationError\] PubMed returns only the first 9999 records of a search; offset 9999 is past "
                r"them, so narrow the query$"
            ),
        ):
            await pubmed.search(client, "cancer", size=10, offset=9999)
        assert sent == []
        # Positive control: the last offset PubMed serves is asked for and read.
        total, records = await pubmed.search(client, "cancer", size=10, offset=9998)
    assert (total, records) == (5714011, [EXPECTED])
    assert sent[0].url.params["retstart"] == "9998"


def _linking_ncbi(links: dict[str, list[str]], sent: list[httpx.Request]):
    """An NCBI that links PMID 42802988 to ``links[db]`` and summarises any uid list the way
    NCBI does, including its 500-uid limit for JSON esummary."""

    def summary(db: str, uid: str) -> dict[str, Any]:
        if db == "sra":
            return {"uid": uid, "expxml": f'<Experiment acc="SRX{uid}"/>', "runs": ""}
        if db == "gds":
            return {"uid": uid, "accession": f"GSE{uid}"}
        return {"uid": uid, "project_acc": f"PRJNA{uid}"}

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        endpoint, params = _endpoint(request), request.url.params
        if endpoint == "elink.fcgi":
            db = params["db"]
            return httpx.Response(
                200,
                json={
                    "linksets": [
                        {
                            "dbfrom": "pubmed",
                            "ids": ["42802988"],
                            "linksetdbs": [{"dbto": db, "links": links[db]}],
                        }
                    ]
                },
            )
        if endpoint == "esummary.fcgi":
            db, uids = params["db"], params["id"].split(",")
            if db == "pubmed":
                return httpx.Response(200, json={"result": {"uids": uids, "42802988": LIVE_DOC}})
            if len(uids) > 500:
                return httpx.Response(200, json=TOO_MANY_UIDS)
            return httpx.Response(
                200, json={"result": {"uids": uids, **{u: summary(db, u) for u in uids}}}
            )
        if endpoint == "efetch.fcgi":
            return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>")
        return httpx.Response(200, json={"resultList": {"result": [{"inEPMC": "N"}]}})  # EuropePMC

    return answer


async def test_a_paper_linking_more_records_than_the_cap_resolves_with_a_truncation_note() -> None:
    cap = omics.MAX_LINKED_RUNS
    sra = [str(10_000 + i) for i in range(600)]
    gds = [str(20_000 + i) for i in range(cap)]  # exactly the cap: whole, no note
    bioproject = ["30000", "30001"]
    sent: list[httpx.Request] = []
    answer = _linking_ncbi({"sra": sra, "gds": gds, "bioproject": bioproject}, sent)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        r = await pubmed.resolve(client, "pubmed:42802988")
    assert [lnk.target_id for lnk in r.links] == [
        *(f"sra:SRX{u}" for u in sra[:cap]),
        *(f"geo:GSE{u}" for u in gds),
        "bioproject:PRJNA30000",
        "bioproject:PRJNA30001",
    ]
    assert {lnk.rel for lnk in r.links} == {"has_data"}
    assert r.truncated == {"links": f"first {cap} of 600 linked sra records"}
    summarised = {
        r.url.params["db"]: r.url.params["id"].split(",")
        for r in sent
        if _endpoint(r) == "esummary.fcgi"
    }
    assert summarised == {
        "pubmed": ["42802988"],
        "sra": sra[:cap],
        "gds": gds,
        "bioproject": bioproject,
    }
    # Positive control: under the cap in every db, the record holds every link and no note.
    sent.clear()
    answer = _linking_ncbi({"sra": sra[:3], "gds": [], "bioproject": bioproject}, sent)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        whole = await pubmed.resolve(client, "pubmed:42802988")
    assert len(whole.links) == 5 and whole.truncated == {}
    assert whole.model_dump(exclude={"links"}) == EXPECTED.model_dump(exclude={"links"})


async def test_the_note_names_every_capped_db() -> None:
    cap = omics.MAX_LINKED_RUNS
    sent: list[httpx.Request] = []
    links = {
        "sra": [str(i) for i in range(cap + 1)],
        "gds": [],
        "bioproject": [str(i) for i in range(cap + 7)],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_linking_ncbi(links, sent))
    ) as client:
        r = await pubmed.resolve(client, "pubmed:42802988")
    assert r.truncated == {
        "links": f"first {cap} of {cap + 1} linked sra records; first {cap} of {cap + 7} linked bioproject records"
    }
    assert len(r.links) == 2 * cap


_WRONG = ["x", "", 7, True, 1.5, [1], {"k": 1}, None]
_ABSENT = object()


def _paths(node: Any, path: tuple = ()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(path: tuple, value: object) -> dict[str, Any]:
    doc = copy.deepcopy(LIVE_DOC)
    *parents, last = path
    node: Any = doc
    for p in parents:
        node = node[p]
    if value is _ABSENT:
        del node[last]
    else:
        node[last] = value
    return doc


def test_no_wrong_typed_field_escapes_as_a_bare_error() -> None:
    """`_eutils` checks that each uid has an object; the fields of that object were read
    unchecked, so a wrong-typed one escaped as a bare TypeError/AttributeError or a pydantic
    error. Every field of a verbatim record, set to every JSON type or removed, must be
    refused as upstream trouble or read cleanly."""
    assert pubmed._is_doc(LIVE_DOC)
    assert pubmed._normalize_pubmed(LIVE_DOC) == EXPECTED
    escaped, refused = [], set()
    for path in _paths(LIVE_DOC):
        if not path:
            continue
        for value in [*_WRONG, *([_ABSENT] if isinstance(path[-1], str) else [])]:
            doc = _with(path, value)
            try:
                pubmed._normalize_pubmed(doc)
            except UpstreamUnavailableError as exc:
                assert str(exc) == (
                    "[UpstreamUnavailableError] NCBI esummary (pubmed) returned a malformed "
                    f"record: {doc!r:.200}"
                )
                refused.add(path)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []
    # The check refuses only the fields the reader reads, not the record's other fields.
    assert refused == {
        ("uid",),
        ("authors",),
        ("authors", 0),
        ("authors", 0, "name"),
        ("title",),
        ("articleids",),
        ("articleids", 0),
        ("articleids", 0, "idtype"),
        ("articleids", 0, "value"),
        ("sortpubdate",),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uid", ""),
        ("uid", "42802988,1"),
        ("uid", None),
        ("title", None),
        ("sortpubdate", 2026),
        ("authors", [{"name": None}]),
        ("authors", ["Imani M"]),
        ("authors", None),
        ("articleids", [{"idtype": "doi", "value": 10}]),
        ("articleids", [{"idtype": 3, "value": "10.1/x"}]),
        ("articleids", ["10.1/x"]),
        ("articleids", None),
    ],
)
def test_each_checked_field_is_refused_at_the_wrong_type(field: str, value: object) -> None:
    assert pubmed._is_doc(LIVE_DOC)  # positive control
    assert not pubmed._is_doc(LIVE_DOC | {field: value})


def test_a_record_without_its_optional_fields_reads_cleanly() -> None:
    doc = {"uid": "7", "title": ""}
    assert pubmed._is_doc(doc)
    assert pubmed._normalize_pubmed(doc) == DataResource(
        id="pubmed:7", source="pubmed", kind="publication", title="", identifiers={"pmid": "7"}
    )


async def test_a_malformed_record_in_a_search_page_is_upstream_trouble() -> None:
    bad = LIVE_DOC | {"authors": "Imani M"}

    def answer(request: httpx.Request) -> httpx.Response:
        if _endpoint(request) == "esearch.fcgi":
            return httpx.Response(
                200, json={"esearchresult": {"count": "1", "idlist": ["42802988"]}}
            )
        return httpx.Response(200, json={"result": {"uids": ["42802988"], "42802988": doc}})

    doc = LIVE_DOC
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        assert await pubmed.search(client, "q") == (1, [EXPECTED])  # positive control
        doc = bad
        with pytest.raises(
            UpstreamUnavailableError, match=r"NCBI esummary \(pubmed\) returned a malformed record"
        ):
            await pubmed.search(client, "q")


@live_only
async def test_live_a_paper_linking_thousands_of_sra_records_resolves() -> None:
    # PMID 42544407 links 7,498 SRA records (live 2026-10-02); the old code raised a bare
    # httpx.InvalidURL building one esummary URL for all of them.
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await pubmed.resolve(client, "pubmed:42544407")
    assert re.fullmatch(r"first 100 of \d{4,} linked sra records", r.truncated["links"])
    assert sum(re.fullmatch(r"sra:[SED]RX\d+", lnk.target_id) is not None for lnk in r.links) == 100
    assert len({lnk.target_id for lnk in r.links}) == len(r.links)


@live_only
async def test_live_pubmed_search_stops_at_its_window() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        # The premise, from NCBI: one past the last offset is an error in a 200 whose raw
        # newline makes it invalid JSON.
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body"):
            await _eutils.esearch(client, "pubmed", "cancer", retmax=1, retstart=9999)
        total, records = await pubmed.search(client, "cancer", size=1, offset=9998)
        assert total > 9999 and len(records) == 1
        with pytest.raises(ValidationError, match="first 9999 records"):
            await pubmed.search(client, "cancer", size=1, offset=9999)


@live_only
async def test_live_ncbi_reads_a_pmid_as_a_list_and_we_do_not_send_one() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        # The premise, from NCBI: a comma makes two records, a word an in-band error.
        both = await _eutils.esummary(client, "pubmed", ["34320281,1"])
        assert [d["uid"] for d in both] == ["34320281", "1"]
        with pytest.raises(UpstreamUnavailableError, match="Invalid uid abc"):
            await _eutils.esummary(client, "pubmed", ["abc"])
        for pmid in ("34320281,1", "abc"):
            with pytest.raises(NotFoundError, match="a PMID is a decimal number"):
                await pubmed.resolve(client, f"pubmed:{pmid}")
