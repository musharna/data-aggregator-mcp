import os

import httpx
import pytest

from data_aggregator_mcp import nasacmr, router
from data_aggregator_mcp import server as server_mod
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, Link

_ITEM_FULL = {
    "meta": {"concept-id": "C2586786218-POCLOUD", "revision-date": "2026-05-28T00:09:09.450Z"},
    "umm": {
        "EntryTitle": "GHRSST Level 4 OSTIA Global SST Analysis",
        "ShortName": "OSTIA-UKMO-L4-GLOB-REP-v2.0",
        "Version": "2.0",
        "Abstract": "A sea surface temperature analysis.",
        "DOI": {"DOI": "10.5067/GHOST-4RM02", "Authority": "https://doi.org/"},
        "DataCenters": [{"ShortName": "NASA/JPL/PODAAC"}, {"ShortName": "NASA/JPL/PODAAC"}],
        "ScienceKeywords": [
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": "OCEAN TEMPERATURE"}
        ],
        "UseConstraints": {
            "Description": "This conforms to the CCBY License (http://creativecommons.org/licenses/by/4.0/)."
        },
        "RelatedUrls": [
            {"URL": "https://podaac.jpl.nasa.gov/cite", "Type": "VIEW RELATED INFORMATION"},
            {"URL": "https://search.earthdata.nasa.gov/search?q=OSTIA", "Type": "GET DATA"},
        ],
        "DataDates": [{"Date": "2019-01-01T00:00:00.000Z", "Type": "CREATE"}],
    },
}
_ITEM_NOLIC = {
    "meta": {"concept-id": "C999-PROV", "revision-date": "2025-01-01T00:00:00Z"},
    "umm": {"EntryTitle": "No-licence dataset", "DOI": {"DOI": "doi:10.16904/x"}},
}
_SEARCH = {"hits": 5184, "took": 12, "items": [_ITEM_FULL, _ITEM_NOLIC]}


@pytest.mark.asyncio
async def test_search_normalizes_collection():
    async def handler(request):
        assert request.url.path.endswith("/collections.umm_json")
        assert request.url.params["keyword"] == "sst"
        assert request.url.params["page_size"] == "10"
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await nasacmr.search(c, "sst", size=10)

    assert total == 5184
    r0 = recs[0]
    assert r0.id == "nasacmr:C2586786218-POCLOUD" and r0.source == "nasacmr"
    assert r0.kind == "dataset" and r0.doi == "10.5067/GHOST-4RM02"
    assert r0.creators == [Creator(name="NASA/JPL/PODAAC")]  # deduped
    assert r0.subjects == ["OCEAN TEMPERATURE"]  # most specific keyword leaf
    assert r0.license == "CC-BY-4.0" and r0.access == "open"  # CC URL pulled from free text
    assert r0.last_updated == "2026-05-28T00:09:09.450Z" and r0.year == 2019
    assert r0.files == []  # discovery-only: no fetchable collection file
    assert r0.links == [
        Link(rel="data_access", target_id="https://search.earthdata.nasa.gov/search?q=OSTIA")
    ]


@pytest.mark.asyncio
async def test_doi_prefix_and_missing_license():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        _total, recs = await nasacmr.search(c, "x", size=10)
    r1 = recs[1]
    assert r1.doi == "10.16904/x"  # "doi:" prefix stripped
    assert r1.license is None and r1.access is None  # no UseConstraints → never guessed


def test_strip_doi_forms():
    assert nasacmr._strip_doi("doi:10.5067/x") == "10.5067/x"
    assert nasacmr._strip_doi("10.5067/x") == "10.5067/x"
    assert nasacmr._strip_doi(None) is None
    # a prefix-only value must yield None, not "" (audit L3)
    assert nasacmr._strip_doi("doi:") is None
    assert nasacmr._strip_doi("DOI: ") is None
    assert nasacmr._strip_doi("") is None


def test_normalize_survives_schema_violations():
    """A schema-violating response — a non-string UseConstraints leaf, and null elements in
    the UMM list fields — must degrade gracefully, not crash the whole search leg (audit L1/L2)."""
    item = {
        "meta": {"concept-id": "C1-X"},
        "umm": {
            "EntryTitle": "t",
            "UseConstraints": {"Description": {"nested": "object not a string"}},
            "DataCenters": [None, {"ShortName": "NASA/X"}],
            "ScienceKeywords": [None, {"Term": "TEMPERATURE"}],
            "RelatedUrls": [None, {"Type": "GET DATA", "URL": "https://earthdata/x"}],
            "DataDates": [None, {"Type": "CREATE", "Date": "2020-06-01"}],
        },
    }
    r = nasacmr._normalize(item)  # must not raise
    assert r.license is None  # the bad leaf is ignored, not joined
    assert [c.name for c in r.creators] == ["NASA/X"]
    assert r.subjects == ["TEMPERATURE"]
    assert r.links and r.links[0].target_id == "https://earthdata/x"
    assert r.year == 2020


def test_pub_year_prefers_create_over_update():
    """DataDates entries are unordered and carry mixed types; the publication year must come
    from a CREATE/PRODUCTION date, not merely the first one listed (audit L4)."""
    umm = {
        "DataDates": [
            {"Type": "UPDATE", "Date": "2025-01-01"},
            {"Type": "CREATE", "Date": "2019-01-01"},
        ]
    }
    assert nasacmr._pub_year(umm) == 2019
    # falls back to any parseable date when no CREATE/PRODUCTION is present
    assert nasacmr._pub_year({"DataDates": [{"Type": "UPDATE", "Date": "2025-01-01"}]}) == 2025


@pytest.mark.asyncio
async def test_search_caps_page_size():
    captured: list[str] = []

    async def handler(request):
        captured.append(request.url.params["page_size"])
        return httpx.Response(200, json={"hits": 0, "items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await nasacmr.search(c, "x", size=9999)
    assert captured == [str(nasacmr.MAX_SIZE)]


# --- resolve ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_concept_id():
    def handler(request):
        assert request.url.params["concept_id"] == "C2586786218-POCLOUD"
        return httpx.Response(200, json={"hits": 1, "items": [_ITEM_FULL]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await nasacmr.resolve(c, "nasacmr:C2586786218-POCLOUD")
    assert r.id == "nasacmr:C2586786218-POCLOUD" and r.doi == "10.5067/GHOST-4RM02"
    assert r.files == []  # still discovery-only after resolve


@pytest.mark.asyncio
async def test_resolve_missing_raises_not_found():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"hits": 0, "items": []}))
    ) as c:
        with pytest.raises(NotFoundError):
            await nasacmr.resolve(c, "nasacmr:C0-NONE")


# --- registration wiring (no network) -----------------------------------------


def test_nasacmr_is_registered_and_selectable():
    assert "nasacmr" in router.available_sources()
    assert router._select(["nasacmr"]) == {"nasacmr": nasacmr}


def test_nasacmr_is_discovery_only_not_in_fetch_gate():
    assert not server_mod._is_fetchable("nasacmr:C2586786218-POCLOUD")
    by_name = {s["name"]: s for s in server_mod._SOURCES}
    assert by_name["nasacmr"]["fetchable"] is False


def test_nasacmr_registered_after_gwas_for_dedup_precedence():
    order = list(router._ADAPTERS)
    assert order.index("nasacmr") > order.index("gwas")


# --- live (opt-in) ------------------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await nasacmr.search(c, "sea surface temperature", size=5)
    assert total > 0 and recs
    assert recs[0].id.startswith("nasacmr:") and recs[0].source == "nasacmr"


@_live_only
@pytest.mark.asyncio
async def test_live_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await nasacmr.search(c, "sea surface temperature", size=5)
        r = await nasacmr.resolve(c, recs[0].id)
    assert r.id == recs[0].id and r.title
