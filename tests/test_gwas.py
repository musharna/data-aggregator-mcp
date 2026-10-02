import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import gwas
from data_aggregator_mcp.errors import NotFoundError

# Verbatim GWAS Catalog REST API v2 responses (captured 2026-09-28): the v1 API these
# tests used to mimic was retired and answers every request with HTTP 429.
_FIX = Path(__file__).parent / "fixtures"
_SEARCH = json.loads((_FIX / "gwas_v2_search.json").read_text())  # disease_trait=asthma
_RECORD = json.loads((_FIX / "gwas_v2_study.json").read_text())  # GCST000028
_PUBLICATION = json.loads((_FIX / "gwas_v2_publication.json").read_text())  # PMID 17463246


def _study_handler(*, publication=None):
    """Serve GCST000028 and its publication; ``publication`` overrides that response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v2/studies/GCST000028"):
            return httpx.Response(200, json=_RECORD)
        if request.url.path.endswith("/api/v2/publications/17463246"):
            return publication or httpx.Response(200, json=_PUBLICATION)
        return httpx.Response(404, json={"errorCode": 404, "error": "Not Found"})

    return handler


@pytest.mark.asyncio
async def test_search_normalizes_studies():
    seen: list[str] = []

    async def handler(request):
        seen.append(request.url.path)
        assert request.url.path.endswith("/gwas/rest/api/v2/studies")
        assert request.url.params.get("disease_trait") == "asthma"
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await gwas.search(c, "asthma", size=2)
    assert total == 87
    assert [r.id for r in recs] == ["gwas:GCST90480249", "gwas:GCST90476698"]
    r0 = recs[0]
    assert r0.source == "gwas" and r0.kind == "study"
    # A v2 study carries no publication; search does not spend a call per row on one,
    # so the row is titled by its trait (resolve fills the paper title and year).
    assert r0.title == "Asthma" and r0.year is None
    assert r0.identifiers.get("pmid") == "39024449"
    assert "Asthma" in r0.subjects
    assert len(seen) == 1  # one request for the page, none per study


@pytest.mark.asyncio
async def test_search_paginates_by_page_number():
    captured = {}

    async def handler(request):
        captured["page"] = request.url.params.get("page")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await gwas.search(c, "asthma", size=10, offset=20)
    assert captured["page"] == "2"


@pytest.mark.asyncio
async def test_resolve_normalizes_study_with_its_publication():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_study_handler())) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r.id == "gwas:GCST000028" and r.kind == "study"
    assert r.title == (
        "Genome-wide association analysis identifies loci for type 2 diabetes and "
        "triglyceride levels."
    )
    assert r.year == 2007 and r.last_updated == "2007-04-26"
    assert r.identifiers.get("pmid") == "17463246"
    assert "Type 2 diabetes" in r.subjects and r.files == []
    assert r.errors == {}


@pytest.mark.asyncio
async def test_resolve_wrong_shape_publication_is_a_failed_lookup(monkeypatch):
    # A 200 whose body is not a publication object is a failure, not "no publication".
    from data_aggregator_mcp import _http

    async def _no_sleep(*_a, **_k):  # the wrong shape is retried; skip the real backoff
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    wrong = httpx.Response(200, json=[_PUBLICATION])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_study_handler(publication=wrong))
    ) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r.title == "Type 2 diabetes" and r.year is None
    assert "GWAS Catalog publication" in r.errors["publication"]


@pytest.mark.asyncio
async def test_resolve_publication_the_catalog_lacks_is_an_answer():
    # 404 on /publications: the study stands, titled by its trait, and nothing failed.
    missing = httpx.Response(404, json={"errorCode": 404, "error": "Not Found"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_study_handler(publication=missing))
    ) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r.title == "Type 2 diabetes" and r.year is None
    assert r.errors == {}


@pytest.mark.asyncio
async def test_router_does_not_cache_a_failed_publication_lookup(monkeypatch):
    """The publication is a second request; when it fails the study is still returned
    (titled by its trait) but says so in errors["publication"], and router.resolve does
    not cache it. A study whose lookups answered is cached."""
    from data_aggregator_mcp import _http, router

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    ok_study = copy.deepcopy(_RECORD)
    ok_study["accession_id"], ok_study["pubmed_id"] = "GCST000029", 17463249
    ok_pub = dict(_PUBLICATION, pubmed_id="17463249")
    study_gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/studies/" in path:
            acc = path.rsplit("/", 1)[-1]
            study_gets.append(acc)
            return httpx.Response(200, json=_RECORD if acc == "GCST000028" else ok_study)
        if path.endswith("/publications/17463246"):
            return httpx.Response(500, json={"status": 500, "error": "Internal Server Error"})
        return httpx.Response(200, json=ok_pub)

    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        bad = await router.resolve(c, "gwas:GCST000028")
        await router.resolve(c, "gwas:GCST000028")
        ok = await router.resolve(c, "gwas:GCST000029")
        await router.resolve(c, "gwas:GCST000029")
    assert bad.title == "Type 2 diabetes" and bad.year is None
    assert "GWAS Catalog publication" in bad.errors["publication"]
    assert study_gets.count("GCST000028") == 2  # not cached: the second resolve re-fetched
    # Positive control: the publication answered, so the record is complete and cached.
    assert ok.year == 2007 and ok.errors == {} and study_gets.count("GCST000029") == 1


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    body = {"errorCode": 404, "error": "Not Found", "errorMessage": "Studies not found"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json=body))
    ) as c:
        with pytest.raises(NotFoundError):
            await gwas.resolve(c, "gwas:GCST999999")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await gwas.resolve(c, "gwas:")


def test_registered_discovery_only():
    from data_aggregator_mcp import router, server

    assert "gwas" in router.available_sources()
    assert router._ADAPTERS["gwas"] is gwas
    assert "gwas:" not in server._FETCHABLE_SOURCES
    assert any(s["name"] == "gwas" for s in server._SOURCES)


# ---------------------------------------------------------------------------
# Fix — page math must use capped size, not raw size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_page_math_uses_capped_size():
    """size=100 (>MAX_SIZE=50) with offset=100 → page=2 (100//50), size param=50."""
    captured: dict = {}

    async def handler(request):
        captured["page"] = request.url.params.get("page")
        captured["size"] = request.url.params.get("size")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await gwas.search(c, "asthma", size=100, offset=100)

    assert captured["size"] == "50", f"expected size=50, got {captured['size']!r}"
    assert captured["page"] == "2", f"expected page=2, got {captured['page']!r}"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gwas.search(c, "asthma", size=3)
        assert total > 0 and recs and recs[0].id.startswith("gwas:")
        full = await gwas.resolve(c, recs[0].id)
    assert full.kind == "study" and full.identifiers.get("pmid")
    # The publication lookup answered: the paper title and year are on the record.
    assert full.errors == {} and full.year and full.title != "Asthma"


@_live_only
@pytest.mark.asyncio
async def test_live_trait_search_is_exact_as_the_source_declares():
    """sources.py tells the model the query must be an exact GWAS trait term. Check the
    upstream still behaves that way: every hit's trait is the query, and a non-term
    finds nothing (not a fuzzy match)."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gwas.search(c, "Type 2 diabetes", size=10)
        none_total, none_recs = await gwas.search(c, "Type 2 diabetes mellitus xyz", size=3)
    assert total > 0 and recs
    assert {s.lower() for r in recs for s in r.subjects} == {"type 2 diabetes"}
    assert (none_total, none_recs) == (0, [])


@pytest.mark.asyncio
async def test_search_offset_not_on_page_boundary_starts_at_offset():
    """M7 (audit 2026-09-22): offset=3,size=10 fetched page 0 and returned GCST000..,
    repeating three records the router had already consumed. The page-boundary slice
    (zenodo/dandi/datacite pattern) must drop the first ``offset % size`` records."""
    accs = [f"GCST{i:03d}" for i in range(25)]

    def handler(req: httpx.Request) -> httpx.Response:
        size, page = int(req.url.params["size"]), int(req.url.params["page"])
        chunk = accs[page * size : (page + 1) * size]
        return httpx.Response(
            200,
            json={
                "_embedded": {"studies": [{"accession_id": a} for a in chunk]},
                "page": {"totalElements": len(accs)},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await gwas.search(c, "x", size=10, offset=3)
        # positive control: a page-aligned offset is unchanged
        _, aligned = await gwas.search(c, "x", size=10, offset=10)
    assert total == 25
    assert [r.id for r in recs] == [f"gwas:{a}" for a in accs[3:10]]
    assert [r.id for r in aligned] == [f"gwas:{a}" for a in accs[10:20]]


@pytest.mark.asyncio
async def test_search_404_is_an_outage_not_zero_hits(monkeypatch):
    """H3 class: `not_found_returns={}` on the SEARCH endpoint turned a moved/removed
    endpoint into "0 studies". A search 404 now raises; a real empty page is still 0."""

    empty = {"_embedded": {"studies": []}, "page": {"totalElements": 0}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await gwas.search(c, "height")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=empty))
    ) as c:
        assert await gwas.search(c, "height") == (0, [])


@pytest.mark.asyncio
async def test_every_request_waits_past_the_slowest_reply_the_catalog_gives():
    """v2 answers in 21-33 s; successful replies arrived at 31.6 and 32.7 s (probe
    2026-09-28 21:50 EDT). The 30 s timeout abandoned them, and each retry was cut at
    the same 30 s, so a trait search failed "unreachable after 3 tries" (nightly-live
    run 36508819844). Every request (search, study, publication) must wait longer
    than the slowest reply seen."""
    slowest_reply_seen = 32.7
    read_timeouts: dict[str, float] = {}
    study = _study_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts[request.url.path.rsplit("/", 2)[-2]] = request.extensions["timeout"]["read"]
        if request.url.path.endswith("/api/v2/studies"):
            return httpx.Response(200, json=_SEARCH)
        return study(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await gwas.search(c, "asthma", size=2)
        r = await gwas.resolve(c, "gwas:GCST000028")
    # positive control: the same requests still parse into records
    assert total == 87 and len(recs) == 2
    assert r.year == 2007 and r.errors == {}
    assert set(read_timeouts) == {"v2", "studies", "publications"}
    assert all(t > slowest_reply_seen for t in read_timeouts.values()), read_timeouts


@_live_only
@pytest.mark.asyncio
async def test_live_search_at_the_largest_tool_size_is_answered():
    """The search tool allows 50 per call, and a live test at size 3 cannot see an
    upstream cap (the zenodo lesson). Asthma has more than 50 studies, so a full page
    comes back and every row passes the page check."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gwas.search(c, "asthma", size=50)
    assert total > 50 and len(recs) == 50
    assert all(r.id.startswith("gwas:GCST") for r in recs)


@_live_only
@pytest.mark.asyncio
async def test_live_page_past_the_end_is_empty_not_malformed():
    """Past the last page the Catalog leaves out `_embedded`; the page check must read
    that as an empty page with the real total, not as a broken answer."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await gwas.search(c, "asthma", size=10, offset=1000)
    assert 50 < total < 1000 and recs == []
