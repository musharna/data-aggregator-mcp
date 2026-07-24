import os

import httpx
import pytest

from data_aggregator_mcp import datagov, router
from data_aggregator_mcp import server as server_mod
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

# --- search -------------------------------------------------------------------

_PKG_FULL = {
    "id": "cb6a67f2-7211-4f6e-addf-0ccb4ac14d47",
    "name": "civil-rights-data-collection-crdc",
    "title": "Civil Rights Data Collection (CRDC)",
    "notes": "<p>Since 1968, OCR has collected  civil rights data.</p>",
    "license_id": "us-pd",
    "organization": {"title": "Department of Education"},
    "tags": [{"name": "civil-rights"}, {"name": "education"}],
    "metadata_created": "2020-11-10T00:00:00.000Z",
    "metadata_modified": "2024-03-02T00:00:00.000Z",
    "resources": [
        {
            "name": "CRDC 2020.csv",
            "url": "https://example.gov/crdc-2020.csv",
            "format": "CSV",
            "mimetype": "text/csv",
            "size": 12345,
            "hash": "",
        },
        {"name": "landing", "url": "https://example.gov/page", "format": "HTML"},
    ],
}
_PKG_CC0 = {
    "name": "cc0-dataset",
    "title": "Open data",
    "license_id": "cc-zero",
    "license_url": "http://www.opendefinition.org/licenses/cc-zero",
    "resources": [],
}
_PKG_UNSPEC = {"name": "unspec", "title": "Unlicensed", "license_id": "notspecified"}

_SEARCH = {
    "success": True,
    "result": {"count": 22986, "results": [_PKG_FULL, _PKG_CC0, _PKG_UNSPEC]},
}


@pytest.mark.asyncio
async def test_search_normalizes_and_sends_api_key(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key-123")
    seen_headers: dict[str, str] = {}

    async def handler(request):
        assert request.url.path.endswith("/action/package_search")
        assert request.url.params["q"] == "rights"
        assert request.url.params["rows"] == "10"
        seen_headers.update(request.headers)
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await datagov.search(c, "rights", size=10)

    assert seen_headers.get("x-api-key") == "test-key-123"  # env key, not DEMO_KEY
    assert total == 22986
    r0 = recs[0]
    assert r0.id == "datagov:civil-rights-data-collection-crdc" and r0.source == "datagov"
    assert r0.kind == "dataset" and r0.doi is None
    assert r0.creators == [Creator(name="Department of Education")]
    assert r0.subjects == ["civil-rights", "education"]
    assert r0.description == "Since 1968, OCR has collected civil rights data."  # HTML stripped
    assert r0.access == "open" and r0.license is None  # us-pd → open, no clean SPDX
    assert r0.last_updated == "2024-03-02T00:00:00.000Z" and r0.year == 2020
    assert r0.files == []  # compact() drops files in the search view


@pytest.mark.asyncio
async def test_license_and_access_mapping(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        _total, recs = await datagov.search(c, "x", size=10)
    cc0 = recs[1]
    assert cc0.license == "CC0-1.0" and cc0.access == "open"
    unspec = recs[2]
    assert unspec.license is None and unspec.access is None  # never guessed open


def test_int_size_coerces_ckan_string_sizes():
    """CKAN sends resource `size` as a numeric string when populated; it must be coerced to
    int, not dropped (audit L5)."""
    assert datagov._int_size("12345") == 12345
    assert datagov._int_size(12345) == 12345
    assert datagov._int_size("  99  ") == 99
    assert datagov._int_size("not-a-number") is None
    assert datagov._int_size(None) is None
    assert datagov._int_size(True) is None  # bool is an int subclass but never a real size


@pytest.mark.asyncio
async def test_resolve_coerces_string_resource_size(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    pkg = {
        "success": True,
        "result": {
            "name": "n",
            "title": "t",
            "resources": [{"name": "f.csv", "url": "https://x/f.csv", "size": "4096"}],
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=pkg))
    ) as c:
        r = await datagov.resolve(c, "datagov:n")
    assert r.files[0].size == 4096  # string "4096" coerced, not dropped to None


def test_api_key_falls_back_to_demo_key(monkeypatch):
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    assert datagov._api_key() == "DEMO_KEY"
    monkeypatch.setenv("DATA_GOV_API_KEY", "real")
    assert datagov._api_key() == "real"


@pytest.mark.asyncio
async def test_search_caps_rows_at_max(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    captured: list[str] = []

    async def handler(request):
        captured.append(request.url.params["rows"])
        return httpx.Response(200, json={"success": True, "result": {"count": 0, "results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await datagov.search(c, "x", size=9999)
    assert captured == [str(datagov.MAX_SIZE)]


# --- resolve ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_populates_resources(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")

    def handler(request):
        assert request.url.path.endswith("/action/package_show")
        assert request.url.params["id"] == "civil-rights-data-collection-crdc"
        return httpx.Response(200, json={"success": True, "result": _PKG_FULL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await datagov.resolve(c, "datagov:civil-rights-data-collection-crdc")

    assert len(r.files) == 2  # both resources have a url
    f = r.files[0]
    assert f.name == "CRDC 2020.csv" and f.url == "https://example.gov/crdc-2020.csv"
    assert f.mime == "text/csv" and f.size == 12345
    assert f.checksum is None  # no reliable upstream checksum → unverified fetch


@pytest.mark.asyncio
async def test_resolve_metadata_only_has_no_files(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    pkg = {"success": True, "result": {"name": "meta", "title": "t", "resources": []}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=pkg))
    ) as c:
        r = await datagov.resolve(c, "datagov:meta")
    assert r.files == []


@pytest.mark.asyncio
async def test_resolve_404_raises_not_found(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="not found"))
    ) as c:
        with pytest.raises(NotFoundError):
            await datagov.resolve(c, "datagov:missing")


@pytest.mark.asyncio
async def test_resolve_success_false_raises_not_found(monkeypatch):
    """CKAN can answer 200 with success:false for a missing package — treat as 404."""
    monkeypatch.setenv("DATA_GOV_API_KEY", "k")
    body = {"success": False, "error": {"message": "Not found"}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        with pytest.raises(NotFoundError):
            await datagov.resolve(c, "datagov:gone")


# --- registration wiring (no network) -----------------------------------------


def test_datagov_is_registered_and_selectable():
    assert "datagov" in router.available_sources()
    assert router._select(["datagov"]) == {"datagov": datagov}


def test_datagov_prefix_is_declared():
    assert "datagov" in datagov.PREFIXES


def test_list_sources_reports_datagov_with_auth():
    by_name = {s["name"]: s for s in server_mod._SOURCES}
    assert "datagov" in by_name
    assert by_name["datagov"]["auth_required"] is True
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
    assert total > 0 and recs
    assert recs[0].id.startswith("datagov:") and recs[0].source == "datagov"


@_live_only
@pytest.mark.asyncio
async def test_live_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await datagov.search(c, "climate", size=5)
        r = await datagov.resolve(c, recs[0].id)
    assert r.id == recs[0].id and r.title
