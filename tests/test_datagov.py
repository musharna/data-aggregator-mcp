import json
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import datagov, router
from data_aggregator_mcp import server as server_mod
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

# Real catalog responses captured 2026-09-27 (descriptions cut to 240 chars, at most 3
# distributions per dataset): GET catalog.data.gov/search?q=water (hits 40, 52, 7, 90 of
# 100) and GET catalog.data.gov/api/dataset/<nyc-climate-budgeting slug>. The live-gated
# tests at the bottom drive the real catalog (the system-boundary check).  # BOUNDARY_OK
_FIX = Path(__file__).parent / "fixtures"
_SEARCH = json.loads((_FIX / "datagov_search.json").read_text())
_DATASET = json.loads((_FIX / "datagov_dataset.json").read_text())
_NYC = "nyc-climate-budgeting-report-climate-alignment-assessment-and-capital-climate-investments"


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Default to the keyless route; tests that exercise the gateway set a key."""
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)


# --- search -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_maps_dcat_hits_from_the_keyless_catalog():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await datagov.search(c, "water", size=4)

    # 4 hits and upstream still has an `after` cursor → lower bound 4 + 1.
    assert total == 5 and len(recs) == 4
    r0 = recs[0]
    assert r0.id == (
        "datagov:urban-retail-water-supplier-water-conservation-supply-and-demand-june-2014-onwards"
    )
    assert r0.source == "datagov" and r0.kind == "dataset" and r0.doi is None
    assert r0.title.startswith("Urban Retail Water Supplier - Water Conservation")
    assert r0.creators == [Creator(name="State of California")]
    assert r0.subjects[:3] == ["Conservation", "Urban Water Use", "urban water supplier"]
    assert r0.license == "ODC-By-1.0"  # opendefinition.org/licenses/odc-by
    assert r0.access == "open"  # accessLevel "public"
    assert r0.year == 2024 and r0.last_updated == "2026-09-14T18:02:36.897643"
    assert r0.files == []  # compact() drops files in the search view
    nonpublic = recs[1]
    assert nonpublic.access == "closed" and nonpublic.license == "CC0-1.0"
    assert nonpublic.subjects[-1] == "geospatial"  # DCAT theme appended after keywords
    no_licence = recs[2]
    assert no_licence.license is None and no_licence.access == "open"
    bare_year = recs[3]  # issued absent, modified "2021"
    assert bare_year.year == 2021 and bare_year.access == "closed"

    (req,) = seen
    assert str(req.url).startswith("https://catalog.data.gov/search?")
    assert req.url.params["q"] == "water" and req.url.params["per_page"] == "4"
    assert "after" not in req.url.params and "x-api-key" not in req.headers


@pytest.mark.asyncio
async def test_search_uses_the_keyed_gateway_when_a_key_is_set(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key-123")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        _total, recs = await datagov.search(c, "water", size=4)

    assert len(recs) == 4
    (req,) = seen
    assert str(req.url).startswith("https://api.gsa.gov/technology/datagov/v4/search?")
    assert req.headers["x-api-key"] == "test-key-123"  # env key, never DEMO_KEY


def _paged_upstream(n: int, seen: list[httpx.Request]):
    """An upstream holding ``n`` hits that pages like the catalog: ``per_page`` rows
    after the opaque ``after`` cursor, ``after`` present only when more remain."""
    template = _SEARCH["results"][0]
    hits = [{**template, "slug": f"ds-{i:03d}"} for i in range(n)]

    def handler(request):
        seen.append(request)
        start = int(request.url.params.get("after", "0"))
        per_page = int(request.url.params["per_page"])
        page = hits[start : start + per_page]
        body: dict = {"results": page, "sort": "relevance"}
        if start + per_page < n:
            body["after"] = str(start + per_page)
        return httpx.Response(200, json=body)

    return handler


@pytest.mark.asyncio
async def test_search_serves_the_router_offset_by_walking_the_after_cursor(monkeypatch):
    monkeypatch.setattr(datagov, "_PAGE_MAX", 7, raising=False)  # force a multi-request walk
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_paged_upstream(25, seen))) as c:
        total, recs = await datagov.search(c, "x", size=10, offset=10)
        assert [r.id for r in recs] == [f"datagov:ds-{i:03d}" for i in range(10, 20)]
        assert total == 21  # 20 seen + more upstream
        # Walk: 7 + 7 + 6 rows, each request carrying the previous response's cursor.
        assert [r.url.params["per_page"] for r in seen] == ["7", "7", "6"]
        assert [r.url.params.get("after") for r in seen] == [None, "7", "14"]

        # Positive control: page 1 is one request, no cursor.
        seen.clear()
        total, recs = await datagov.search(c, "x", size=5)
        assert [r.id for r in recs] == [f"datagov:ds-{i:03d}" for i in range(5)]
        assert total == 6 and len(seen) == 1

        # Last page: fewer rows than asked, no cursor → the total is exact.
        seen.clear()
        total, recs = await datagov.search(c, "x", size=10, offset=20)
        assert [r.id for r in recs] == [f"datagov:ds-{i:03d}" for i in range(20, 25)]
        assert total == 25


@pytest.mark.asyncio
async def test_search_caps_size_at_max():
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_paged_upstream(0, seen))) as c:
        total, recs = await datagov.search(c, "x", size=9999)
    assert (total, recs) == (0, [])
    assert [r.url.params["per_page"] for r in seen] == [str(datagov.MAX_SIZE)]


# --- resolve ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_maps_distributions_to_files():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_DATASET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await datagov.resolve(c, f"datagov:{_NYC}")

    assert r.id == f"datagov:{_NYC}" and r.creators == [Creator(name="City of New York")]
    assert r.year == 2025 and r.last_updated == "2026-08-11" and r.access == "open"
    assert "City Government" in r.subjects  # DCAT theme
    views = "https://data.cityofnewyork.us/api/v3/views/c99a-c5ux"
    assert [(f.mime, f.url) for f in r.files] == [
        ("application/json", f"{views}/query.json?accessType=DOWNLOAD"),
        # An XML mediaType keeps fetch's HTML sniff armed for this file.
        ("application/xml", f"{views}/query.xml?accessType=DOWNLOAD"),
        ("text/csv", f"{views}/export.csv?accessType=DOWNLOAD"),
    ]
    assert all(f.checksum is None and f.source == "datagov" for f in r.files)
    (req,) = seen
    assert str(req.url) == f"https://catalog.data.gov/api/dataset/{_NYC}"


@pytest.mark.asyncio
async def test_resolve_uses_gateway_route_when_keyed_and_quotes_the_id(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_DATASET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await datagov.resolve(c, f"datagov:{_NYC}")
        await datagov.resolve(c, "datagov:../search")
    assert seen[0].url.path == f"/technology/datagov/v4/dataset/{_NYC}"
    assert seen[0].headers["x-api-key"] == "k"
    # A slash in the id is encoded: it stays one path segment under /dataset/.
    assert seen[1].url.raw_path == b"/technology/datagov/v4/dataset/..%2Fsearch"


def test_files_fall_back_to_access_url_and_skip_url_less_distributions():
    hit = _SEARCH["results"][2]  # water-shortage-vulnerability-datasets: accessURL only
    files = datagov._files(hit["dcat"])
    assert [f.url for f in files] == [d["accessURL"] for d in hit["dcat"]["distribution"]]
    assert files[0].name == "Download - Water Shortage Vulnerability (Sections)"
    assert datagov._files({"distribution": [{"title": "no url"}]}) == []
    assert datagov._files(_SEARCH["results"][3]["dcat"]) == []  # no distributions


def test_license_mapping():
    lic = datagov._license
    assert lic({"license": "http://www.opendefinition.org/licenses/odc-by"}) == "ODC-By-1.0"
    assert lic({"license": "http://www.opendefinition.org/licenses/cc-zero"}) == "CC0-1.0"
    assert lic({"license": "https://creativecommons.org/licenses/by/4.0"}) == "CC-BY-4.0"
    # Versionless CC and agency-specific licences are never given a guessed SPDX id.
    assert lic({"license": "http://www.opendefinition.org/licenses/cc-by"}) is None
    assert lic({"license": "https://edg.epa.gov/EPA_Data_License.html"}) is None
    assert lic({}) is None


@pytest.mark.asyncio
async def test_resolve_missing_dataset_raises_not_found():
    """The catalog answers a missing slug with 404 + an empty result list."""
    empty = {"aggregations": None, "results": [], "search_after": None, "total": 0}

    def handler(request):
        if request.url.path.endswith(f"/{_NYC}"):
            return httpx.Response(200, json=_DATASET)
        return httpx.Response(404, json=empty)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert (await datagov.resolve(c, f"datagov:{_NYC}")).title  # positive control
        with pytest.raises(NotFoundError):
            await datagov.resolve(c, "datagov:no-such-dataset")

    # A 200 carrying no result is also "not found", never an empty record.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=empty))
    ) as c:
        with pytest.raises(NotFoundError):
            await datagov.resolve(c, "datagov:gone")


# --- registration wiring (no network) -----------------------------------------


def test_datagov_is_registered_and_selectable():
    assert "datagov" in router.available_sources()
    assert router._select(["datagov"]) == {"datagov": datagov}


def test_datagov_prefix_is_declared():
    assert "datagov" in datagov.PREFIXES


def test_list_sources_reports_datagov_keyless():
    by_name = {s["name"]: s for s in server_mod._SOURCES}
    assert "datagov" in by_name
    assert by_name["datagov"]["auth_required"] is False
    assert by_name["datagov"]["fetchable"] == "per-dataset"


def test_datagov_is_in_the_fetch_gate():
    assert server_mod._is_fetchable("datagov:civil-rights-data-collection-crdc")


# --- live (opt-in) ------------------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await datagov.search(c, "climate", size=5)
    assert total > len(recs) == 5  # a full page with more upstream
    assert recs[0].id.startswith("datagov:") and recs[0].source == "datagov"
    assert all(r.title for r in recs) and any(r.subjects for r in recs)


@_live_only
@pytest.mark.asyncio
async def test_live_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        _total, recs = await datagov.search(c, "climate", size=5)
        r = await datagov.resolve(c, recs[0].id)
        # A CKAN-era dataset name still resolves in the new catalog.
        crdc = await datagov.resolve(c, "datagov:civil-rights-data-collection-crdc")
    assert r.id == recs[0].id and r.title
    assert crdc.id == "datagov:civil-rights-data-collection-crdc" and crdc.title


@_live_only
@pytest.mark.asyncio
async def test_live_offset_walk_matches_one_long_page():
    async with httpx.AsyncClient(timeout=60) as c:
        _t, long_page = await datagov.search(c, "water", size=10)
        _t, second = await datagov.search(c, "water", size=5, offset=5)
    assert [r.id for r in second] == [r.id for r in long_page[5:10]]
