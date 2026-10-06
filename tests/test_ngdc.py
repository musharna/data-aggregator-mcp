"""The NGDC adapter against verbatim answers of NGDC's search endpoint (captured
2026-10-06), and live.

The round-2 benchmark's five studies held only in NGDC (R1 S13 S14, R2 S5 S10, R4 S2)
were found by no arm; NGDC's own search finds each for the task's words.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import ngdc, router
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_FIX = Path(__file__).parent / "fixtures"
# q=((axolotl OR axolotls)) AND attrs.Center:"GSA", start=0, length=3
_SEARCH = json.loads((_FIX / "ngdc_search.json").read_text())
_EMPTY = json.loads((_FIX / "ngdc_search_empty.json").read_text())
# q=attrs.Accession:"PRJCA022406"
_RESOLVE = json.loads((_FIX / "ngdc_resolve.json").read_text())
# The answer to a query NGDC cannot run (a date range), with HTTP 200.
_ERROR = json.loads((_FIX / "ngdc_error.json").read_text())
LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(ngdc._http.asyncio, "sleep", instant)


def _client(sent: list[httpx.Request], body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _search(body: dict, query: str = "axolotl", **kw):
    sent: list[httpx.Request] = []
    async with _client(sent, body) as c:
        return await ngdc.search(c, query, **kw), sent


@pytest.mark.asyncio
async def test_search_asks_for_ngdcs_own_projects_and_reads_them():
    (total, recs), sent = await _search(_SEARCH, size=3)
    assert [dict(r.url.params) for r in sent] == [
        {
            "db": "bioproject",
            "q": '((axolotl OR axolotls)) AND attrs.Center:"GSA"',
            "sort": "desc",
            "start": "0",
            "length": "3",
        }
    ]
    assert sent[0].headers["accept"] == "application/json"
    assert total == 10
    assert [r.id for r in recs] == ["ngdc:PRJCA038634", "ngdc:PRJCA003076", "ngdc:PRJCA020953"]
    first = recs[0]
    assert first.description.startswith("Axolotls underwent partial nephrectomy")
    assert (first.source, first.kind, first.year) == ("ngdc", "study", 2026)
    assert first.title == "Studies of nephron regeneration after nephrectomy in Axolotl"
    assert first.accessions == ["CRA024672"]
    assert first.organism == ["Ambystoma mexicanum"]
    # "Transcriptome or Gene expression  Raw sequence reads" is two data types.
    assert first.subjects == ["Transcriptome or Gene expression", "Raw sequence reads"]
    assert recs[1].subjects == ["Transcriptome or Gene expression"]
    assert first.last_updated == "2026-06-05"
    assert [(link.rel, link.target_id) for link in first.links] == [
        ("landing_page", "https://ngdc.cncb.ac.cn/bioproject/browse/PRJCA038634")
    ]


@pytest.mark.asyncio
async def test_search_pages_by_offset_caps_the_length_and_can_send_the_query_as_written():
    _, sent = await _search(_SEARCH, query="snow leopard", size=200, offset=60, plurals=False)
    params = sent[0].url.params
    assert (params["start"], params["length"]) == ("60", "50")
    assert params["q"] == '(snow leopard) AND attrs.Center:"GSA"'


@pytest.mark.asyncio
async def test_no_match_is_an_empty_page():
    (total, recs), _ = await _search(_EMPTY)
    assert (total, recs) == (0, [])


@pytest.mark.asyncio
async def test_the_error_envelope_ngdc_answers_with_http_200_is_upstream_trouble():
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] NGDC search returned an unparseable 200 body "
        r"after 3 tries: UpstreamEnvelopeError\(\"no NGDC BioProject list in \{'code': '500'",
    ):
        await _search(_ERROR)
    # Positive control: the verbatim page is read.
    (total, _), _ = await _search(_SEARCH)
    assert total == 10


@pytest.mark.asyncio
async def test_an_insdc_mirror_in_the_page_means_the_native_filter_failed():
    """The query keeps NGDC's own projects; a PRJNA row means it no longer does, and the
    page fails loud instead of returning NCBI's projects under ngdc: ids."""
    leaked = copy.deepcopy(_SEARCH)
    leaked["result"]["data"]["data"][1]["id"] = "PRJNA757699"
    with pytest.raises(UpstreamUnavailableError, match="no NGDC BioProject list"):
        await _search(leaked)


@pytest.mark.asyncio
async def test_resolve_reads_the_project_by_accession():
    sent: list[httpx.Request] = []
    async with _client(sent, _RESOLVE) as c:
        # Whitespace around the accession and its case are not part of it.
        r = await ngdc.resolve(c, "ngdc: prjca022406 ")
    assert dict(sent[0].url.params) == {
        "db": "bioproject",
        "q": 'attrs.Accession:"PRJCA022406"',
        "sort": "desc",
        "start": "0",
        "length": "5",
    }
    assert r.id == "ngdc:PRJCA022406"
    assert r.title == "Conservation genomics of global snow leopards"
    assert (r.year, r.organism) == (2025, ["Panthera uncia"])
    assert sorted(r.accessions) == ["CRA021509", "CRA022084"]


@pytest.mark.asyncio
async def test_resolve_of_an_unknown_or_malformed_id_is_not_found():
    sent: list[httpx.Request] = []
    async with _client(sent, _EMPTY) as c:
        with pytest.raises(NotFoundError, match="NGDC has no BioProject PRJCA999999999"):
            await ngdc.resolve(c, "ngdc:PRJCA999999999")
        with pytest.raises(NotFoundError, match="malformed NGDC BioProject id"):
            await ngdc.resolve(c, 'ngdc:PRJCA1" OR "x')
    # The malformed id never reached the network.
    assert [r.url.params["q"] for r in sent] == ['attrs.Accession:"PRJCA999999999"']


@pytest.mark.asyncio
async def test_resolve_names_itself_when_ngdc_answers_the_error_envelope():
    sent: list[httpx.Request] = []
    async with _client(sent, _ERROR) as c:
        with pytest.raises(
            UpstreamUnavailableError, match=r"^\[UpstreamUnavailableError\] NGDC resolve "
        ):
            await ngdc.resolve(c, "ngdc:PRJCA022406")


def _row(**change) -> dict:
    """The first verbatim search row with ``change`` applied (a value of ``...`` deletes
    the key)."""
    row = copy.deepcopy(_SEARCH["result"]["data"]["data"][0])
    for key, value in change.items():
        if value is ...:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def _page(*rows) -> dict:
    body = copy.deepcopy(_SEARCH)
    body["result"]["data"]["data"] = list(rows)
    return body


# Rows of the wrong shape: each is refused as a whole page, so nothing reads a field at a
# type it does not have.
_MALFORMED = [
    pytest.param("PRJCA038634", id="row-not-an-object"),
    pytest.param(_row(id=38634), id="id-not-a-string"),
    pytest.param(_row(id="PRJCA038634x"), id="id-not-an-accession"),
    pytest.param(_row(title=...), id="no-title"),
    pytest.param(_row(description=["a"]), id="description-not-a-string"),
    pytest.param(_row(species="Ambystoma mexicanum"), id="species-a-string"),
    pytest.param(_row(species=["Ambystoma mexicanum", 7]), id="species-holds-a-number"),
    pytest.param(_row(attrs=["GSA"]), id="attrs-not-an-object"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _MALFORMED)
async def test_a_row_of_the_wrong_shape_fails_the_page(row):
    with pytest.raises(UpstreamUnavailableError, match="no NGDC BioProject list"):
        await _search(_page(row))
    # Positive control: the same page with the verbatim row is read.
    (_, recs), _ = await _search(_page(_row()))
    assert [r.id for r in recs] == ["ngdc:PRJCA038634"]


@pytest.mark.asyncio
async def test_a_row_without_its_optional_fields_reads_as_empty():
    bare = _row(species=None, attrs=None, description="")
    attrs = copy.deepcopy(_row()["attrs"])
    attrs["CrasAcc"] = ["CRA1", 7, "  "]  # only the accession is one
    (_, recs), _ = await _search(_page(bare, _row(id="PRJCA1", attrs=attrs)))
    first = recs[0]
    assert (first.organism, first.accessions, first.subjects) == ([], [], [])
    assert (first.description, first.year, first.last_updated) == (None, None, None)
    assert recs[1].accessions == ["CRA1"]


# --- live ------------------------------------------------------------------------------

_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "want"),
    [
        ("snow leopard", {"ngdc:PRJCA022406"}),  # R4 S2
        ("axolotl", {"ngdc:PRJCA002093", "ngdc:PRJCA052336"}),  # R2 S5, S10
        ("broomrape", {"ngdc:PRJCA015315"}),  # R1 S14
        ("orobanche", {"ngdc:PRJCA008847"}),  # R1 S13
    ],
)
async def test_live_the_benchmark_studies_held_only_in_ngdc_are_found(query, want):
    async with httpx.AsyncClient(timeout=60) as c:
        page = await router.search_page(c, query=query, sources=["ngdc"], size=50)
    assert want <= {r.id for r in page.results}, [r.id for r in page.results]
    # NGDC answered; other keys (NCBI's taxonomy lookup, rate-limited) are other upstreams.
    assert "ngdc" not in page.errors, page.errors


@_live
@pytest.mark.asyncio
async def test_live_resolve_of_the_advertised_example():
    async with httpx.AsyncClient(timeout=60) as c:
        r = await ngdc.resolve(c, "ngdc:PRJCA022406")
    assert r.organism == ["Panthera uncia"] and "CRA021509" in r.accessions
