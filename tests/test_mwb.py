"""The Metabolomics Workbench adapter against verbatim answers of its REST API (captured
2026-10-11), and live.

Round 3 (2026-10-11): the coral heat-stress studies ST004143 and ST004329 (T5 S3, S4)
were past OmicsDI's copy of the Workbench, which stops at ST002490, and dam never saw
them; web and ToolUniverse found both. The Workbench's own title search returns both.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from data_aggregator_mcp import _mirror, mwb, router
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, Link

_FIX = Path(__file__).parent / "fixtures"
LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
# study/study_title/<word>/summary, verbatim. "coral" holds 4, one of them "Strongyloides
# stercoralis"; "stercoralis" holds that one alone, which the API answers unwrapped.
_TITLE = {
    w: json.loads((_FIX / f"mwb_title_{w}.json").read_text())
    for w in ("coral", "heat", "acropora", "stercoralis")
}
# study/study_id/ST004329/summary
_STUDY = json.loads((_FIX / "mwb_study_ST004329.json").read_text())
# The answer to a word in no title, and to an id no study has.
_EMPTY = json.loads((_FIX / "mwb_empty.json").read_text())
# An unknown output item: HTTP 200, text/html.
_ERROR = (_FIX / "mwb_error.html").read_text()
_PREFIX = "/rest/study/"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(mwb._http.asyncio, "sleep", instant)


def _client(sent: list[str], answers: dict[str, object]):
    """A client answering ``<field>/<value>`` (decoded) from ``answers``; a str is sent as
    text/html, anything else as JSON, and an unlisted request as the empty answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        assert path.startswith(_PREFIX) and path.endswith("/summary"), path
        key = path[len(_PREFIX) : -len("/summary")]
        sent.append(key)
        assert request.headers["accept"] == "application/json"
        body = answers.get(key, _EMPTY)
        if isinstance(body, str):
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _titles(**words: object) -> dict[str, object]:
    return {f"study_title/{w}": body for w, body in words.items()}


async def _search(query: str, answers: dict[str, object] | None = None, **kw):
    sent: list[str] = []
    answers = _titles(**_TITLE) if answers is None else answers
    async with _client(sent, answers) as c:
        return await mwb.search(c, query, **kw), sent


@pytest.mark.asyncio
async def test_search_keeps_the_studies_naming_the_word_and_reads_them():
    (total, recs), sent = await _search("coral")
    assert sent == ["study_title/coral"]
    # "Strongyloides stercoralis" holds the substring, not the word; newest first.
    assert total == 3
    assert [r.id for r in recs] == ["mwb:ST004329", "mwb:ST004143", "mwb:ST002933"]
    first = recs[0]
    assert first.title == (
        "Acute heat stress redirects coral carbon budgets: integrative evidence from "
        "physiology and metabolomics"
    )
    assert (first.source, first.kind, first.year) == ("mwb", "study", 2025)
    assert first.organism == ["Acropora hyacinthus", "Pocillopora damicornis", "Porites lutea"]
    assert (first.subjects, first.accessions) == (["LC-MS"], ["ST004329"])
    assert (first.license, first.last_updated) == ("CC-BY-4.0", "2025-11-07")
    # The record's study_url names the searched word ("StudyID=CORAL"), not the study.
    assert [(link.rel, link.target_id) for link in first.links] == [
        (
            "landing_page",
            "https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?Mode=Study&StudyID=ST004329",
        )
    ]


@pytest.mark.asyncio
async def test_each_word_is_its_own_title_search_and_a_study_must_name_them_all():
    (total, recs), sent = await _search("coral heat")
    assert sorted(sent) == ["study_title/coral", "study_title/heat"]
    # ST004143 (coral, "Thermal") and every heat-only study fall away.
    assert (total, [r.id for r in recs]) == (1, ["mwb:ST004329"])
    # A word may be named by the species: "Acropora" is in no title of the heat studies,
    # but ST004329's species is Acropora hyacinthus.
    (total, recs), sent = await _search("acropora heat")
    assert sorted(sent) == ["study_title/acropora", "study_title/heat"]
    assert [r.id for r in recs] == ["mwb:ST004329"]
    # Positive control: one of the words alone keeps them all.
    (total, _), _ = await _search("acropora")
    assert total == 3


@pytest.mark.asyncio
async def test_a_short_word_is_required_but_not_sent_unless_it_is_all_there_is():
    # "lc" is in most titles as a substring; it is matched in the analysis type.
    (total, recs), sent = await _search("coral lc")
    assert sent == ["study_title/coral"]
    assert [r.id for r in recs] == ["mwb:ST004329", "mwb:ST004143"]  # ST002933 is GC-MS
    (total, recs), sent = await _search("lc", _titles(lc=_TITLE["coral"]))
    assert sent == ["study_title/lc"]
    # Of the four coral-titled studies, the three LC-MS ones name "lc".
    assert [r.id for r in recs] == ["mwb:ST004329", "mwb:ST004143", "mwb:ST004137"]


@pytest.mark.asyncio
async def test_operators_quotes_and_punctuation_are_read_as_the_server_reads_them():
    # A quoted phrase is one title search, and so is a hyphenated word, read as its words
    # in order ("single-cell" names "single cell"); OR is no word.
    _, sent = await _search('"heat stress" OR coral-reef')
    assert sorted(sent) == ["study_title/coral reef", "study_title/heat stress"]


@pytest.mark.asyncio
async def test_one_hit_comes_unwrapped_and_none_comes_as_an_empty_list():
    (total, recs), _ = await _search("stercoralis")
    assert (total, [r.id for r in recs]) == (1, ["mwb:ST004137"])
    (total, recs), _ = await _search("zzqxnomatch")
    assert (total, recs) == (0, [])


@pytest.mark.asyncio
async def test_search_pages_by_offset_and_caps_the_page():
    answers = _titles(heat=_TITLE["heat"])
    (total, recs), _ = await _search("heat", answers, size=3, offset=2)
    (_, every), _ = await _search("heat", answers, size=200)
    assert len(every) == total == 20  # 27 titles hold "heat"; 7 only inside "wheat" and the like
    assert [r.id for r in recs] == [r.id for r in every[2:5]]
    assert every == sorted(every, key=lambda r: r.id, reverse=True)
    many = {f"ST{i:06d}": dict(_STUDY, study_id=f"ST{i:06d}") for i in range(1, 80)}
    (total, recs), _ = await _search("heat", _titles(heat=many), size=200)
    assert (total, len(recs)) == (79, mwb.MAX_SIZE)


@pytest.mark.asyncio
async def test_a_query_with_no_word_is_refused_without_a_request():
    with pytest.raises(ValueError, match="needs a word to look for in titles; 'the OR of'"):
        await _search("the OR of")
    # Positive control: one word is enough.
    (_, recs), sent = await _search("the coral")
    assert sent == ["study_title/coral"] and recs


@pytest.mark.asyncio
async def test_an_html_answer_is_upstream_trouble_not_an_empty_page():
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] Metabolomics Workbench search returned an "
        r"unparseable 200 body",
    ):
        await _search("coral", _titles(coral=_ERROR))
    # Positive control: the verbatim page is read.
    (total, _), _ = await _search("coral")
    assert total == 3


def _study(**change) -> dict:
    """The verbatim ST004329 summary with ``change`` applied (``...`` deletes the key)."""
    s = copy.deepcopy(_STUDY)
    for key, value in change.items():
        if value is ...:
            s.pop(key, None)
        else:
            s[key] = value
    return s


_MALFORMED = [
    pytest.param(["ST004329"], id="non-empty-list"),
    pytest.param({"1": "ST004329"}, id="row-not-an-object"),
    pytest.param({"detail": "Internal error"}, id="error-envelope"),
    pytest.param({"1": _study(study_id=4329)}, id="id-not-a-string"),
    pytest.param({"1": _study(study_id="ST4329")}, id="id-not-an-accession"),
    pytest.param({"1": _study(study_title=...)}, id="no-title"),
    pytest.param({"1": _study(species=["Acropora"])}, id="species-not-a-string"),
    pytest.param(_study(license=4), id="unwrapped-license-not-a-string"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED)
async def test_an_answer_of_the_wrong_shape_fails_the_search(body):
    with pytest.raises(UpstreamUnavailableError, match="no Workbench study list"):
        await _search("coral", _titles(coral=body))
    # Positive control: the same answer with the verbatim study is read.
    (_, recs), _ = await _search("coral", _titles(coral={"1": _study()}))
    assert [r.id for r in recs] == ["mwb:ST004329"]


@pytest.mark.asyncio
async def test_a_study_without_its_optional_fields_reads_as_empty():
    bare = _study(species=..., analysis_type="", release_date=..., license=..., institute=...)
    semi = _study(study_id="ST000002", species="Homo sapiens; Mus musculus；Danio rerio")
    dash = _study(study_id="ST000001", species="-")  # 4 of 4,623 studies (2026-10-11)
    (_, recs), _ = await _search("coral", _titles(coral={"1": bare, "2": semi, "3": dash}))
    first, second, third = recs
    assert (first.organism, first.subjects, first.year, first.license) == ([], [], None, None)
    assert first.last_updated is None
    assert second.organism == ["Homo sapiens", "Mus musculus", "Danio rerio"]
    assert third.organism == []


@pytest.mark.asyncio
async def test_resolve_reads_the_study_by_id():
    sent: list[str] = []
    async with _client(sent, {"study_id/ST004329": _STUDY}) as c:
        # Whitespace around the id and its case are not part of it.
        r = await mwb.resolve(c, "mwb: st004329 ")
    assert sent == ["study_id/ST004329"]
    assert (r.id, r.year, r.organism[0]) == ("mwb:ST004329", 2025, "Acropora hyacinthus")


@pytest.mark.asyncio
async def test_resolve_of_an_unknown_or_malformed_id_is_not_found():
    sent: list[str] = []
    async with _client(sent, {}) as c:
        with pytest.raises(NotFoundError, match="Metabolomics Workbench has no study ST999999"):
            await mwb.resolve(c, "mwb:ST999999")
        for bad in ("mwb:ST00432", "mwb:ST004329/../x", "mwb:MTBLS1"):
            with pytest.raises(NotFoundError, match="malformed Metabolomics Workbench study id"):
                await mwb.resolve(c, bad)
        # An id the API answers with a different study is not that study.
        async with _client(sent, {"study_id/ST004320": _STUDY}) as c2:
            with pytest.raises(NotFoundError, match="has no study ST004320"):
                await mwb.resolve(c2, "mwb:ST004320")
    # The malformed ids never reached the network.
    assert sent == ["study_id/ST999999", "study_id/ST004320"]


# --- one deposit, two sources -------------------------------------------------------------


def _omicsdi_copy(acc: str) -> DataResource:
    """OmicsDI's record of a Workbench study, as its search reads one."""
    return DataResource(
        id=f"omicsdi:metabolomics_workbench:{acc}",
        source="omicsdi",
        kind="study",
        title="Metabolomic profiling of plasma in diabetes patients",
        accessions=[acc],
        links=[Link(rel="landing_page", target_id=f"https://www.omicsdi.org/x/{acc}")],
    )


@pytest.mark.asyncio
async def test_a_study_and_omicsdis_copy_of_it_are_one_deposit():
    (_, recs), _ = await _search("coral")
    native = recs[0]
    assert _mirror.deposit_key(native) == _mirror.deposit_key(_omicsdi_copy("ST004329"))
    assert _mirror.deposit_key(native) == "study:ST004329"
    kept = _mirror.dedup_by_deposit([native, _omicsdi_copy("ST004329"), recs[1]])
    assert [r.id for r in kept] == [
        "omicsdi:metabolomics_workbench:ST004329",  # fetchable source outranks discovery-only
        "mwb:ST004143",
    ]
    # Positive control: another study's copy, and a PRIDE copy, stay their own hits.
    assert len(_mirror.dedup_by_deposit([native, _omicsdi_copy("ST004143")])) == 2
    pride = _omicsdi_copy("PXD006873").model_copy(update={"id": "omicsdi:pride:PXD006873"})
    assert _mirror.deposit_key(pride) is None


@pytest.mark.asyncio
async def test_the_router_names_mwb_when_every_word_empties_it(monkeypatch):
    sent: list[str] = []
    async with _client(sent, _titles(**_TITLE)) as c:
        monkeypatch.setattr(router, "_ADAPTERS", {"mwb": mwb})
        page = await router.search_page(c, query="coral zebrafish", sources=["mwb"])
        assert page.errors["all_words"].startswith("mwb matched nothing: it returns ")
        # Positive control: words a study names are no note.
        page = await router.search_page(c, query="coral heat", sources=["mwb"])
    assert "all_words" not in page.errors
    assert [r.id for r in page.results] == ["mwb:ST004329"]


# --- live ---------------------------------------------------------------------------------

_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "want"),
    [
        ("coral", {"mwb:ST004143", "mwb:ST004329"}),
        ("coral metabolomics", {"mwb:ST004143", "mwb:ST004329"}),
        # Every word is needed: ST004143 says "Thermal", not "heat".
        ("coral heat stress", {"mwb:ST004329"}),
        # "Acropora" is only ST004329's species: found through the title's "heat".
        ("acropora heat", {"mwb:ST004329"}),
    ],
)
async def test_live_the_round_3_coral_studies_are_found(query, want):
    async with httpx.AsyncClient(timeout=90) as c:
        page = await router.search_page(c, query=query, sources=["mwb"], size=50)
    assert "mwb" not in page.errors, page.errors
    assert want <= {r.id for r in page.results}, [r.id for r in page.results]


@_live
@pytest.mark.asyncio
async def test_live_resolve_of_the_advertised_example_and_an_unknown_id():
    async with httpx.AsyncClient(timeout=90) as c:
        r = await mwb.resolve(c, "mwb:ST004329")
        assert "coral" in r.title and r.license == "CC-BY-4.0"
        with pytest.raises(NotFoundError):
            await mwb.resolve(c, "mwb:ST999999")


@_live
@pytest.mark.asyncio
async def test_live_a_study_both_sources_return_is_one_hit():
    """ "diabetes" (2026-10-11): OmicsDI and the Workbench both return 23 studies."""
    async with httpx.AsyncClient(timeout=90) as c:
        page = await router.search_page(c, query="diabetes", sources=["mwb", "omicsdi"], size=50)
    accs = [a for r in page.results for a in r.accessions if a.startswith("ST")]
    assert accs and len(accs) == len(set(accs)), sorted(accs)
    assert {r.source for r in page.results} >= {"mwb", "omicsdi"}
