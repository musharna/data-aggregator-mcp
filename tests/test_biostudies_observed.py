"""BioStudies behaviour the mutation run (nightly-guardrails, #88) showed no test observed.

166 mutants survived test_biostudies.py: the request each call site sends (method,
Accept, timeout, retry budget), the error text a failure carries, the file-list URL
quoting, and most fields of a normalised record, which the existing tests checked for
presence rather than value. Each test below pins one of those; they run against the
same real payloads test_biostudies.py uses.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from data_aggregator_mcp import _http, biostudies
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, FileEntry, Link, Metrics

FX = pathlib.Path(__file__).parent / "fixtures"
STUDY = json.loads((FX / "biostudies_study.json").read_text())
SEARCH = json.loads((FX / "biostudies_search.json").read_text())
FL_STUDY = json.loads((FX / "biostudies_filelist_study.json").read_text())
FILE_LISTS = json.loads((FX / "biostudies_filelists.json").read_text())

_LANDING = "https://www.ebi.ac.uk/biostudies/studies/"
_FILES = "https://www.ebi.ac.uk/biostudies/files/"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


def _server(seen: list[httpx.Request], *, fail: str = "") -> httpx.MockTransport:
    """Search, the S-BIAD8 study and its two file lists. ``fail`` names the one
    endpoint ("search", "study", "list") that answers 503 instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/search"):
            kind, body = "search", SEARCH
        elif path.startswith("/biostudies/api/v1/studies/"):
            kind, body = "study", FL_STUDY
        else:
            kind, body = "list", FILE_LISTS[path.rsplit("/", 1)[-1]]
        return httpx.Response(503) if kind == fail else httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# What each call site sends
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_call_is_a_json_get_with_the_shared_timeout() -> None:
    """Search, study and both file lists: GET, asking for JSON (httpx's own default is
    ``*/*``), under ``_http``'s 30 s timeout rather than none."""
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen)) as c:
        await biostudies.search(c, "drought", size=2)
        await biostudies.resolve(c, "biostudies:S-BIAD8")
    assert [r.url.path for r in seen] == [
        "/biostudies/api/v1/search",
        "/biostudies/api/v1/studies/S-BIAD8",
        "/biostudies/files/S-BIAD8/smlm_data.json",
        "/biostudies/files/S-BIAD8/non-smlm_data.json",
    ]
    for r in seen:
        assert r.method == "GET"
        assert r.headers["Accept"] == "application/json"
        assert r.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 30.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail", "service", "endpoint"),
    [
        ("search", "BioStudies search", "/api/v1/search"),
        ("study", "BioStudies resolve", "/studies/S-BIAD8"),
        # The first list fails, so the second is never asked for.
        ("list", "BioStudies file list", "/S-BIAD8/smlm_data.json"),
    ],
)
async def test_an_outage_is_tried_twice_and_named(
    fail: str, service: str, endpoint: str, no_sleep: None
) -> None:
    """Each call site gets MAX_RETRIES (2) attempts, not ``_http``'s default 3, and the
    error names which BioStudies call failed. Positive control: the same server with
    nothing failing answers both calls."""
    async with httpx.AsyncClient(transport=_server([])) as c:
        assert (await biostudies.search(c, "drought", size=2))[1]
        assert len((await biostudies.resolve(c, "biostudies:S-BIAD8")).files) == 7

    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen, fail=fail)) as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            if fail == "search":
                await biostudies.search(c, "drought", size=2)
            else:
                await biostudies.resolve(c, "biostudies:S-BIAD8")
    assert (
        str(err.value)
        == f"[UpstreamUnavailableError] {service} exhausted 2 retries (last HTTP 503)"
    )
    assert sum(r.url.path.endswith(endpoint) for r in seen) == 2
    assert not any(r.url.path.endswith("non-smlm_data.json") for r in seen)


@pytest.mark.asyncio
async def test_a_file_list_answering_an_error_envelope_is_an_outage(no_sleep: None) -> None:
    """A list endpoint returning ``{"detail": ...}`` where rows were promised is a
    malformed body, never rows: extending by a dict would add its keys, which the file
    walk then drops, and resolve would return a short manifest as the whole study.
    Positive control: the real lists resolve all 7 files."""
    async with httpx.AsyncClient(transport=_server([])) as c:
        assert len((await biostudies.resolve(c, "biostudies:S-BIAD8")).files) == 7

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/biostudies/api/v1/studies/"):
            return httpx.Response(200, json=FL_STUDY)
        return httpx.Response(200, json={"detail": "Internal error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] BioStudies file list returned an unparseable 200 body after 2 tries",
        ):
            await biostudies.resolve(c, "biostudies:S-BIAD8")


@pytest.mark.asyncio
async def test_a_file_list_name_is_quoted_but_keeps_its_directories() -> None:
    """A list name is a relative path: ``/`` stays a separator, while a space or ``#``
    is escaped (unescaped, ``#`` would cut the URL short at a fragment)."""
    study = {
        "accno": "S-X1",
        "section": {"attributes": [{"name": "File List", "value": "sub dir/a#1.json"}]},
    }
    raw: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/biostudies/api/v1/studies/"):
            return httpx.Response(200, json=study)
        raw.append(request.url.raw_path)
        return httpx.Response(200, json=[{"path": "sub dir/x.tif", "size": 3}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await biostudies.resolve(c, "biostudies:S-X1")
    assert raw == [b"/biostudies/files/S-X1/sub%20dir/a%231.json"]
    assert [f.name for f in r.files] == ["sub dir/x.tif"]


@pytest.mark.asyncio
async def test_resolve_trims_whitespace_around_the_accession() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=STUDY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await biostudies.resolve(c, "biostudies: E-GEOD-30436 ")
    assert paths == ["/biostudies/api/v1/studies/E-GEOD-30436"]
    assert r.id == "biostudies:E-GEOD-30436"


@pytest.mark.asyncio
async def test_resolve_errors_name_the_id() -> None:
    """A malformed id and a missing study each say which id; positive control: a
    well-formed, present id resolves through the same client."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/E-GEOD-30436"):
            return httpx.Response(200, json=STUDY)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert (
            await biostudies.resolve(c, "biostudies:E-GEOD-30436")
        ).id == "biostudies:E-GEOD-30436"
        with pytest.raises(NotFoundError) as malformed:
            await biostudies.resolve(c, "biostudies:a/b")
        with pytest.raises(NotFoundError) as missing:
            await biostudies.resolve(c, "biostudies:E-NOPE-1")
    assert str(malformed.value) == "[NotFoundError] malformed BioStudies id 'biostudies:a/b'"
    assert str(missing.value) == "[NotFoundError] BioStudies has no study E-NOPE-1"


@pytest.mark.asyncio
async def test_file_list_guard_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(biostudies, "_MAX_FILE_LISTS", 1)
    async with httpx.AsyncClient(transport=_server([])) as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await biostudies.resolve(c, "biostudies:S-BIAD8")
    assert str(err.value) == (
        "[UpstreamUnavailableError] BioStudies S-BIAD8: 2 file lists, over the 1-list guard; "
        "refusing to return a partial manifest"
    )


@pytest.mark.asyncio
async def test_a_zero_page_size_still_asks_for_page_one() -> None:
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen)) as c:
        _, recs = await biostudies.search(c, "drought", size=0)
    assert seen[0].url.params["page"] == "1"
    assert seen[0].url.params["pageSize"] == "0"
    assert len(recs) == len(SEARCH["hits"])


# --------------------------------------------------------------------------
# Normalised records, field by field
# --------------------------------------------------------------------------


def test_a_search_hit_normalises_in_full() -> None:
    hit = SEARCH["hits"][0]
    assert biostudies._normalize_hit(hit) == DataResource(
        id="biostudies:S-EPMC9542112",
        source="biostudies",
        kind="publication",
        title="Drought legacies and ecosystem responses to subsequent drought.",
        year=2022,
        accessions=["S-EPMC9542112"],
        last_updated="2022-01-01",
        links=[Link(rel="landing_page", target_id=_LANDING + "S-EPMC9542112")],
        metrics=Metrics(views=2),
    )


def test_a_bare_hit_falls_back_without_inventing_values() -> None:
    """No title → the accession; no date → no year and no last_updated; no accession →
    no accession, since relate treats a shared accession as hard evidence."""
    assert biostudies._normalize_hit({"accession": "E-X-1"}) == DataResource(
        id="biostudies:E-X-1",
        source="biostudies",
        kind="study",
        title="E-X-1",
        accessions=["E-X-1"],
        links=[Link(rel="landing_page", target_id=_LANDING + "E-X-1")],
    )
    assert biostudies._normalize_hit({"title": "t"}).accessions == []


def test_a_study_normalises_in_full() -> None:
    sec = {a["name"]: a["value"] for a in STUDY["section"]["attributes"]}
    r = biostudies._normalize_study(STUDY)
    assert (r.id, r.source, r.kind) == ("biostudies:E-GEOD-30436", "biostudies", "study")
    assert r.title == sec["Title"]
    assert r.description == sec["Description"]
    assert r.access == "open"
    assert r.last_updated == "2012-04-29"
    assert r.subjects == ["ArrayExpress", "transcription profiling by array"]
    assert r.accessions == ["E-GEOD-30436", "GSE30436"]
    assert r.identifiers == {"pmid": "22476619", "geo": "GSE30436"}
    assert r.links == [
        Link(rel="landing_page", target_id=_LANDING + "E-GEOD-30436"),
        Link(rel="described_in", target_id="10.1007/s10142-012-0276-1"),
    ]


def test_a_bare_study_falls_back_without_inventing_values() -> None:
    assert biostudies._normalize_study({"accno": "E-X-1"}) == DataResource(
        id="biostudies:E-X-1",
        source="biostudies",
        kind="study",
        title="E-X-1",
        accessions=["E-X-1"],
        access="open",
        links=[Link(rel="landing_page", target_id=_LANDING + "E-X-1")],
    )
    assert biostudies._normalize_study({}).accessions == []


@pytest.mark.parametrize(
    ("section_title", "top_title", "want"),
    [("S", "T", "S"), (None, "T", "T"), (None, None, "E-X-1")],
)
def test_study_title_prefers_section_then_top_then_accession(
    section_title: str | None, top_title: str | None, want: str
) -> None:
    body = {
        "accno": "E-X-1",
        "attributes": [{"name": "Title", "value": top_title}] if top_title else [],
        "section": {
            "attributes": [{"name": "Title", "value": section_title}] if section_title else []
        },
    }
    assert biostudies._normalize_study(body).title == want


# --------------------------------------------------------------------------
# The payload walkers
# --------------------------------------------------------------------------


def test_attrs_skips_junk_and_keeps_empty_values_empty() -> None:
    node = {
        "attributes": [
            "junk",
            {"value": "no name"},
            {"name": "A"},
            {"name": "B", "value": "b1"},
            {"name": "B", "value": "b2"},
        ]
    }
    assert biostudies._attrs(node) == {"A": "", "B": "b2"}


def test_file_lists_skip_junk_blanks_and_repeats() -> None:
    section = {
        "attributes": [
            "junk",
            {"value": "nameless.json"},
            {"name": "File List"},
            {"name": " file list ", "value": " a.json "},
            {"name": "File List", "value": "a.json"},
        ],
        "subsections": [{"attributes": [{"name": "File List", "value": "b.json"}]}],
    }
    assert biostudies._file_lists(section) == ["a.json", "b.json"]


def test_collect_files_skips_junk_and_nameless_rows_and_dedupes() -> None:
    """The walk pops from the end, so junk and a nameless row come first and a
    duplicate comes before x.txt: skipping any of them must not end the walk."""
    section = {
        "files": [
            {"path": "x.txt"},
            {"path": "a.txt", "size": 1},
            {"name": "b.txt"},
            {"path": "a.txt", "size": 1},
            {"path": "c.txt", "size": "7"},
            {"size": 5},
            "junk",
        ]
    }
    got = biostudies._collect_files(section, "S-X1", listed=[{"path": "d.txt", "size": 4}])

    def entry(name: str, size: int | None = None) -> FileEntry:
        return FileEntry(name=name, size=size, url=f"{_FILES}S-X1/{name}", source="biostudies")

    assert sorted(got, key=lambda f: f.name) == [
        entry("a.txt", 1),
        entry("b.txt"),
        entry("c.txt"),
        entry("d.txt", 4),
        entry("x.txt"),
    ]


def test_xrefs_skip_junk_urlless_and_untyped_links() -> None:
    """Links pop from the end, so the junk comes first; skipping it must not end the walk."""
    geo = [{"name": "Type", "value": "GEO"}]
    section = {
        "links": [
            {"url": "GSE1", "attributes": geo},
            {"url": "PRJNA1", "attributes": [{"name": "Type", "value": " ENA "}]},
            {"url": "no-type"},
            "junk",
            {"attributes": geo},
        ]
    }
    assert sorted(biostudies._xrefs(section)) == [("ena", "PRJNA1"), ("geo", "GSE1")]


def _pub(*attrs: tuple[str, str], accno: str | None = None, type_: str | None = "Publication"):
    node: dict = {"attributes": [{"name": n, "value": v} for n, v in attrs]}
    if type_ is not None:
        node["type"] = type_
    if accno is not None:
        node["accno"] = accno
    return {"subsections": [node]}


@pytest.mark.parametrize(
    ("section", "want"),
    [
        (_pub(("DOI", "10.1/a")), ("10.1/a", None)),
        (_pub(("DOI", "10.1/a"), ("doi", "10.1/b")), ("10.1/a", None)),
        (_pub(("Title", "T")), (None, None)),
        (_pub(("PMID", "111"), accno="abc"), (None, "111")),
        (_pub(("PubMed", "111"), accno="abc"), (None, "111")),
        (_pub(("PubMedId", "111"), accno="abc"), (None, "111")),
        (_pub(("PMID", "111"), accno="222"), (None, "111")),
        (_pub(accno="222"), (None, "222")),
        (_pub(accno="abc"), (None, None)),
        (_pub(), (None, None)),
        (_pub(("DOI", "10.1/a"), type_="Author"), (None, None)),
        (_pub(("DOI", "10.1/a"), type_=None), (None, None)),
        ({}, (None, None)),
    ],
)
def test_publication_doi_and_pmid(section: dict, want: tuple[str | None, str | None]) -> None:
    """First DOI and first PMID win; the node's accno is the PMID only when no attribute
    gave one and it is all digits; only a Publication node counts."""
    assert biostudies._publication(section) == want


@pytest.mark.parametrize(
    "name",
    [
        # Live: S-BIAD2787 lists 92 files named like this (2026-10-01); curl rejects the
        # unescaped URL as malformed.
        "Microscopy Images/Figure 8 microscopy/BAs-Leu-A5 merge.tif",
        "well #3.tif",  # unescaped, '#' cut the name: the request was for "well "
        "a?b.csv",  # unescaped, the rest of the name became a query string
        "50%20done.csv",  # unescaped, the server decoded a literal %20 to a space
    ],
)
def test_a_file_url_requests_exactly_the_listed_file(name: str) -> None:
    plain = "plain/dir/x.csv"
    files = biostudies._collect_files({"files": [{"path": plain}, {"path": name}]}, "S-BIAD1")
    urls = {f.name: f.url for f in files}
    base = "https://www.ebi.ac.uk/biostudies/files/S-BIAD1/"
    assert urls[plain] == base + plain  # positive control: nothing to escape
    url = httpx.URL(urls[name])
    assert (url.query, url.fragment) == (b"", "")
    assert url.path == "/biostudies/files/S-BIAD1/" + name
    assert " " not in urls[name]
