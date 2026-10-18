"""A word ending in a colon is sent escaped, not as a field.

Round-3 diagnosis (2026-10-15): the title "BEEHIVE: a public dataset of Apis mellifera
images ..." pasted as a DataCite query found nothing, because DataCite's query language
read "BEEHIVE:" as a field name; without the colon the study ranked first. Zenodo's
query language reads it the same way ("BEEHIVE: a dataset" 298,005 hits, escaped
5,046,709, without the colon 5,046,705).
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import datacite, ngdc, omicsdi, zenodo
from data_aggregator_mcp._relevance import drop_stray_marks, escape_stray_colons

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_BEEHIVE = (
    "BEEHIVE: a public dataset of Apis mellifera images to empower honeybee monitoring research"
)


def test_a_word_ending_in_a_colon_is_escaped() -> None:
    assert escape_stray_colons("BEEHIVE: a dataset") == r"BEEHIVE\: a dataset"
    assert escape_stray_colons("Data from: Varroa  mites:") == r"Data from\: Varroa  mites\:"


def test_field_syntax_and_other_colons_are_sent_as_written() -> None:
    kept = r'title:wolf titles.title:("snow leopard") https://x.org 12:30 a\: b :x'
    assert escape_stray_colons(kept) == kept
    assert escape_stray_colons("honey bee images") == "honey bee images"


def _capture(sent: list[str], key: str, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params[key])
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("plurals", [True, False])
async def test_datacite_and_zenodo_send_the_colon_escaped(plurals: bool) -> None:
    dc: list[str] = []
    zo: list[str] = []
    dc_body = {"data": [], "meta": {"total": 0}}
    zo_body = {"hits": {"hits": [], "total": 0}}
    async with httpx.AsyncClient(transport=_capture(dc, "query", dc_body)) as c:
        await datacite.search(c, "BEEHIVE: bees", plurals=plurals)
        await datacite.search(c, "title:bees", plurals=plurals)  # control: a field
    async with httpx.AsyncClient(transport=_capture(zo, "q", zo_body)) as c:
        await zenodo.search(c, "BEEHIVE: bees", plurals=plurals)
        await zenodo.search(c, "title:bees", plurals=plurals)
    # "bees" ends in "s", so it gets no plural and both forms send the same words.
    assert dc == [r"BEEHIVE\: bees", "title:bees"]
    assert zo == [r"BEEHIVE\: bees", "title:bees"]


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_datacite_finds_a_title_pasted_with_its_colon() -> None:
    async with httpx.AsyncClient() as client:
        _, hits = await datacite.search(client, _BEEHIVE, size=5)
        # Control: the same words without the colon find it, so a miss is the colon's.
        _, plain = await datacite.search(client, _BEEHIVE.replace(":", ""), size=5)
    want = "datacite:10.17632/5yz78xxpmy"
    assert want in {r.id.removesuffix(".1") for r in plain}, [r.id for r in plain]
    assert want in {r.id.removesuffix(".1") for r in hits}, [r.id for r in hits]


def test_a_stray_mark_is_dropped_for_an_upstream_with_no_escape() -> None:
    assert drop_stray_marks("Survival at the edge: Inbreeding", ":") == (
        "Survival at the edge Inbreeding"
    )
    assert drop_stray_marks("SLE: insights; adult", ":;") == "SLE insights adult"
    kept = r"title:wolf TAXONOMY:9606 https://x.org 12:30 a\: b :x a;b"
    assert drop_stray_marks(kept, ":;") == kept
    assert drop_stray_marks("a; b", ":") == "a; b"  # only the marks named are dropped


def _query_part(sent: str) -> str:
    return sent.split(") AND ")[0]


@pytest.mark.parametrize("plurals", [True, False])
async def test_omicsdi_and_ngdc_send_the_colon_dropped(plurals: bool) -> None:
    """Neither reads a backslash escape: OmicsDI and NGDC found 0 records for 5 of 5
    pasted colon titles each, as written and escaped, and each at rank 1 with the colon
    dropped (probed 2026-10-17). NGDC also found 0 for a semicolon, 2 of 2."""
    od: list[str] = []
    ng: list[str] = []
    od_body = {"count": 0, "datasets": []}
    ng_body = {"code": "200", "result": {"data": {"recordsTotal": 0, "data": []}}}
    async with httpx.AsyncClient(transport=_capture(od, "query", od_body)) as c:
        await omicsdi.search(c, "bees: mites; ants", plurals=plurals)
        await omicsdi.search(c, "TAXONOMY:9606", plurals=plurals)  # control: a field
    async with httpx.AsyncClient(transport=_capture(ng, "q", ng_body)) as c:
        await ngdc.search(c, "bees: mites; ants", plurals=plurals)
        await ngdc.search(c, "title:bees", plurals=plurals)
    assert [_query_part(q) for q in od] == ["(bees mites; ants", "(TAXONOMY:9606"]
    assert [_query_part(q) for q in ng] == ["(bees mites ants", "(title:bees"]


_COLON_TITLES = [
    (
        "omicsdi",
        "Multiomics analysis of neutrophils in SLE: insights from adult and pediatric disease",
        "omicsdi:metabolights_dataset:MTBLS6259",
    ),
    (
        "ngdc",
        "Survival at the edge: Inbreeding purging and genomic vulnerability",
        "ngdc:PRJCA042676",
    ),
]


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
@pytest.mark.parametrize(("source", "title", "want"), _COLON_TITLES)
async def test_live_omicsdi_and_ngdc_find_a_title_pasted_with_its_colon(source, title, want):
    mod = {"omicsdi": omicsdi, "ngdc": ngdc}[source]
    async with httpx.AsyncClient() as client:
        _, hits = await mod.search(client, title, size=10)
        # Control: the same words without the colon find it, so a miss is the colon's.
        _, plain = await mod.search(client, title.replace(":", ""), size=10)
    assert want in {r.id for r in plain}, ("without the colon", [r.id for r in plain])
    assert want in {r.id for r in hits}, ("pasted", [r.id for r in hits])


async def test_a_word_freed_of_its_colon_gets_its_plural_like_any_other() -> None:
    sent: list[str] = []
    body = {"code": "200", "result": {"data": {"recordsTotal": 0, "data": []}}}
    async with httpx.AsyncClient(transport=_capture(sent, "q", body)) as c:
        await ngdc.search(c, "leopard: edge")
    assert _query_part(sent[0]) == "((leopard OR leopards) (edge OR edges)"
