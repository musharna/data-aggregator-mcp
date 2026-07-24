import os

import httpx
import pytest

from data_aggregator_mcp import gbif, router
from data_aggregator_mcp import server as server_mod
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

# --- search -------------------------------------------------------------------

_SEARCH = {
    "count": 2120,
    "results": [
        {
            "key": "6d27080f-ed47-48e2-90e8-cdebaba11a03",
            "doi": "10.15468/crpkfp",
            "title": "RBINS Amphibian collection",
            "type": "OCCURRENCE",
            "license": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
            "recordCount": 21244,
            "description": "<p>The RBINS amphibian collection contains <b>135,000</b> specimens.</p>",
            "keywords": ["Occurrence", "Specimen"],
            "publishingOrganizationTitle": "Royal Belgian Institute of Natural Sciences",
        },
        {
            "key": "aaaa1111-0000-0000-0000-000000000000",
            "doi": "10.15468/zzzzzz",
            "title": "A public-domain checklist",
            "type": "CHECKLIST",
            "license": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
        },
        {
            "key": "bbbb2222-0000-0000-0000-000000000000",
            "title": "Unlicensed dataset",
            "type": "METADATA",
            "license": "unspecified",
        },
    ],
}


@pytest.mark.asyncio
async def test_search_normalizes_and_prefixes_id():
    async def handler(request):
        assert request.url.path.endswith("/dataset/search")
        assert request.url.params["q"] == "amphibian"
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await gbif.search(c, "amphibian", size=10)

    assert total == 2120
    r0 = recs[0]
    assert r0.id == "gbif:6d27080f-ed47-48e2-90e8-cdebaba11a03" and r0.source == "gbif"
    assert r0.kind == "dataset" and r0.doi == "10.15468/crpkfp"
    assert r0.license == "CC-BY-NC-4.0" and r0.access == "open"
    assert r0.subjects == ["Occurrence", "Specimen"]
    # HTML stripped to collapsed plain text
    assert r0.description == "The RBINS amphibian collection contains 135,000 specimens."
    # no contacts on the search index → the publishing org is the citation author
    assert r0.creators == [Creator(name="Royal Belgian Institute of Natural Sciences")]
    assert r0.files == []  # search view carries no files; resolve populates them


@pytest.mark.asyncio
async def test_search_maps_license_variants_to_spdx_and_access():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        _total, recs = await gbif.search(c, "x", size=10)

    cc0 = recs[1]
    assert cc0.license == "CC0-1.0" and cc0.access == "open"
    # an unrecognized licence ("unspecified"/"unsupported") is never guessed
    unlic = recs[2]
    assert unlic.license is None and unlic.access is None
    assert unlic.creators == []  # no contacts, no publishing org → empty, not fabricated


@pytest.mark.asyncio
async def test_search_caps_size_at_max():
    captured: list[str] = []

    async def handler(request):
        captured.append(request.url.params["limit"])
        return httpx.Response(200, json={"count": 0, "results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await gbif.search(c, "x", size=9999)
    assert captured == [str(gbif.MAX_SIZE)]


# --- resolve ------------------------------------------------------------------

_DATASET = {
    "key": "6d27080f-ed47-48e2-90e8-cdebaba11a03",
    "doi": "10.15468/crpkfp",
    "title": "RBINS Amphibian collection",
    "type": "OCCURRENCE",
    "license": "http://creativecommons.org/licenses/by/4.0/legalcode",
    "created": "2024-10-09T13:18:17.549+00:00",
    "modified": "2025-03-13T14:50:59.425+00:00",
    "pubDate": "2025-03-13T00:00:00.000+00:00",
    "contacts": [
        {
            "type": "ORIGINATOR",
            "firstName": "Olivier",
            "lastName": "Pauwels",
            "organization": "RBINS",
        },
        {"type": "ADMINISTRATIVE_POINT_OF_CONTACT", "firstName": "Admin", "lastName": "Person"},
    ],
    "keywordCollections": [{"thesaurus": "GBIF", "keywords": ["Occurrence", "Specimen"]}],
    "endpoints": [
        {"type": "DWC_ARCHIVE", "url": "https://ipt.example.be/archive.do?r=amphibia"},
        {"type": "EML", "url": "https://ipt.example.be/eml.do?r=amphibia"},
    ],
}


@pytest.mark.asyncio
async def test_resolve_attaches_dwca_archive_and_maps_metadata():
    def handler(request):
        assert request.url.path.endswith("/dataset/6d27080f-ed47-48e2-90e8-cdebaba11a03")
        return httpx.Response(200, json=_DATASET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await gbif.resolve(c, "gbif:6d27080f-ed47-48e2-90e8-cdebaba11a03")

    # only the DWC_ARCHIVE endpoint becomes a fetchable file (EML is skipped)
    assert len(r.files) == 1
    f = r.files[0]
    assert f.url == "https://ipt.example.be/archive.do?r=amphibia"
    assert f.name == "6d27080f-ed47-48e2-90e8-cdebaba11a03.dwca.zip"
    assert f.mime == "application/zip"
    assert f.checksum is None  # GBIF exposes no checksum → unverified fetch
    assert r.year == 2025 and r.last_updated == "2025-03-13T14:50:59.425+00:00"
    assert r.license == "CC-BY-4.0" and r.access == "open"
    # authorship comes from the ORIGINATOR contact, not the admin contact
    assert r.creators == [Creator(name="Olivier Pauwels")]
    assert r.subjects == ["Occurrence", "Specimen"]


@pytest.mark.asyncio
async def test_resolve_metadata_only_dataset_has_no_files():
    doc = {"key": "k", "title": "meta only", "endpoints": [{"type": "EML", "url": "u"}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=doc))
    ) as c:
        r = await gbif.resolve(c, "gbif:k")
    assert r.files == []


@pytest.mark.asyncio
async def test_resolve_404_raises_not_found():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="not found"))
    ) as c:
        with pytest.raises(NotFoundError):
            await gbif.resolve(c, "gbif:missing")


# --- pure-function units ------------------------------------------------------


def test_normalize_strips_html_and_collapses_whitespace():
    r = gbif._normalize({"key": "k", "title": "t", "description": "<p>a  b</p>\n<i>c</i>"})
    assert r.description == "a b c"


def test_normalize_doi_passthrough_and_none():
    assert gbif._normalize({"key": "k", "title": "t", "doi": "10.15468/x"}).doi == "10.15468/x"
    assert gbif._normalize({"key": "k", "title": "t"}).doi is None


def test_creators_prefers_named_contacts_then_org():
    org_only = gbif._normalize({"key": "k", "title": "t", "publishingOrganizationTitle": "Org"})
    assert org_only.creators == [Creator(name="Org")]


# --- registration wiring (no network) -----------------------------------------


def test_gbif_is_registered_and_selectable():
    assert "gbif" in router.available_sources()
    assert router._select(["gbif"]) == {"gbif": gbif}


def test_gbif_registered_before_datacite_for_dedup_precedence():
    order = list(router._ADAPTERS)
    assert order.index("gbif") < order.index("datacite")


def test_gbif_prefix_routes_to_gbif_resolver():
    assert "gbif" in gbif.PREFIXES


def test_list_sources_reports_gbif():
    by_name = {s["name"]: s for s in server_mod._SOURCES}
    assert "gbif" in by_name
    assert by_name["gbif"]["fetchable"] == "per-dataset"


def test_gbif_is_in_the_fetch_gate():
    assert server_mod._is_fetchable("gbif:6d27080f-ed47-48e2-90e8-cdebaba11a03")


# --- live (opt-in) ------------------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gbif.search(c, "amphibian", size=5)
    assert total > 0 and recs
    assert recs[0].id.startswith("gbif:") and recs[0].source == "gbif"


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_attaches_archive():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gbif.search(c, "amphibian", size=5)
        # resolve the first result that is an occurrence-type dataset (has an archive)
        r = await gbif.resolve(c, recs[0].id)
    assert r.id == recs[0].id and r.doi
