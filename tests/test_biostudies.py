"""BioStudies adapter.

The two JSON fixtures are REAL API responses captured 2026-07-21 (E-GEOD-30436 and
a `drought` search), trimmed only by dropping repeated Author subsections and
capping file lists — the nesting shape is verbatim. That matters here: the first
hand-written mock of this payload passed while missing both the DOI and every file,
because BioStudies buries files under `section.subsections[N].subsections[M].files`
as a list OF LISTS, and the Publication subsection sits ~7 entries deep.
"""

from __future__ import annotations

import json
import os
import pathlib

import httpx
import pytest

from data_aggregator_mcp import biostudies
from data_aggregator_mcp.errors import NotFoundError

FX = pathlib.Path(__file__).parent / "fixtures"
STUDY = json.loads((FX / "biostudies_study.json").read_text())
SEARCH = json.loads((FX / "biostudies_search.json").read_text())


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_normalizes_real_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Exact host, not a substring: `"ebi.ac.uk" in host` also matches
        # ebi.ac.uk.evil.com (CodeQL py/incomplete-url-substring-sanitization).
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["query"] == "drought"
        return httpx.Response(200, json=SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await biostudies.search(c, "drought", size=2)

    assert total == SEARCH["totalHits"]
    assert recs and all(r.id.startswith("biostudies:") for r in recs)
    assert all(r.source == "biostudies" and r.kind == "study" for r in recs)
    # A hit's own accession is addressable, so relate can key on it.
    assert recs[0].accessions == [SEARCH["hits"][0]["accession"]]
    assert all(r.title for r in recs)


@pytest.mark.asyncio
async def test_search_pages_by_page_number_not_row_offset() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await biostudies.search(c, "x", size=10, offset=20)
    # offset 20 @ size 10 is the THIRD page, 1-indexed.
    assert seen["page"] == "3"
    assert seen["pageSize"] == "10"


@pytest.mark.asyncio
async def test_search_first_page_is_page_one() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await biostudies.search(c, "x", size=10, offset=0)
    assert seen["page"] == "1"


@pytest.mark.asyncio
async def test_search_collection_narrows_the_endpoint() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await biostudies.search(c, "x", collection="ArrayExpress")
    assert "/ArrayExpress/search" in urls[0]


@pytest.mark.asyncio
async def test_search_rejects_injected_collection_and_falls_back() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await biostudies.search(c, "x", collection="../../etc/passwd")
    # Falls back to the plain search endpoint rather than interpolating the path.
    assert urls[0].startswith(biostudies.SEARCH)


@pytest.mark.asyncio
async def test_search_caps_page_size() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": [], "totalHits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await biostudies.search(c, "x", size=10_000)
    assert int(seen["pageSize"]) == biostudies.MAX_SIZE


# --------------------------------------------------------------------------
# resolve — against the real payload shape
# --------------------------------------------------------------------------


def test_normalize_study_extracts_doi_pmid_and_geo_xref() -> None:
    r = biostudies._normalize_study(STUDY)
    assert r.id == "biostudies:E-GEOD-30436"
    assert r.doi == "10.1007/s10142-012-0276-1"
    assert r.identifiers["pmid"] == "22476619"
    # The GEO sibling accession is what lets relate connect this to the GEO
    # record the router already indexes.
    assert r.identifiers["geo"] == "GSE30436"
    assert "GSE30436" in r.accessions
    assert "E-GEOD-30436" in r.accessions
    assert r.organism == ["Triticum aestivum"]
    assert r.year == 2012
    assert "ArrayExpress" in r.subjects
    assert any(link.rel == "described_in" for link in r.links)


def test_normalize_study_finds_deeply_nested_files() -> None:
    r = biostudies._normalize_study(STUDY)
    # Files live under section.subsections[N].subsections[M].files, as lists of
    # lists. A flat scan of section.files returns zero.
    assert len(r.files) > 10
    assert all(
        f.url and f.url.startswith("https://www.ebi.ac.uk/biostudies/files/") for f in r.files
    )
    assert any(f.size and f.size > 0 for f in r.files)
    assert len({f.name for f in r.files}) == len(r.files), "file names must be deduped"


def test_files_carry_no_checksum_so_fetch_cannot_overclaim() -> None:
    # BioStudies publishes no md5/sha256. If this ever starts failing, the source
    # gained checksums and the catalog's "UNVERIFIED" note must be revisited.
    r = biostudies._normalize_study(STUDY)
    assert r.files and all(f.checksum is None for f in r.files)
    assert "md5" not in json.dumps(STUDY).lower()


@pytest.mark.asyncio
async def test_resolve_fetches_and_normalizes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/studies/E-GEOD-30436")
        return httpx.Response(200, json=STUDY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await biostudies.resolve(c, "biostudies:E-GEOD-30436")
    assert r.doi == "10.1007/s10142-012-0276-1"
    assert r.files


@pytest.mark.asyncio
async def test_resolve_missing_study_raises_notfound() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as c:
        with pytest.raises(NotFoundError):
            await biostudies.resolve(c, "biostudies:E-NOPE-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["biostudies:", "biostudies:../../secret", "biostudies:a/b"])
async def test_resolve_rejects_malformed_ids_before_network(bad: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be touched for a malformed id")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(NotFoundError):
            await biostudies.resolve(c, bad)


def test_xref_promotion_is_allowlisted() -> None:
    """An unknown link type must NOT become an accession.

    relate reports a shared accession as hard evidence, so a junk value here
    would manufacture a false connection between unrelated records.
    """
    section = {
        "links": [
            [
                {"url": "GSE1", "attributes": [{"name": "Type", "value": "GEO"}]},
                {"url": "http://example.com", "attributes": [{"name": "Type", "value": "Website"}]},
            ]
        ]
    }
    pairs = biostudies._xrefs(section)
    assert pairs == [("geo", "GSE1")]


def test_registered_in_router_and_server() -> None:
    from data_aggregator_mcp import router, server

    assert "biostudies" in router.available_sources()
    assert router._ADAPTERS["biostudies"] is biostudies
    assert "biostudies:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "biostudies" for s in server._SOURCES)


def test_catalog_entry_declares_fetch_unverified() -> None:
    from data_aggregator_mcp import server

    entry = next(s for s in server._SOURCES if s["name"] == "biostudies")
    assert entry["fetchable"] is True
    # The headline promise is verified-fetch-or-fail-loud; a source without
    # checksums must say so rather than imply the guarantee.
    assert "UNVERIFIED" in entry["fetchable_notes"]


# --------------------------------------------------------------------------
# Real execution
# --------------------------------------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve() -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        total, recs = await biostudies.search(c, "drought stress", size=3)
        assert total > 0 and recs
        full = await biostudies.resolve(c, recs[0].id)
        assert full.id == recs[0].id
        assert full.title


@_live_only
@pytest.mark.asyncio
async def test_live_file_url_is_actually_downloadable() -> None:
    """The manifest is worthless if its URLs 404 — check one for real."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        full = await biostudies.resolve(c, "biostudies:E-GEOD-30436")
        target = next(f for f in full.files if f.name.endswith(".txt"))
        assert target.url
        resp = await c.get(target.url, headers={"Range": "bytes=0-200"})
        assert resp.status_code in (200, 206), resp.status_code
        assert resp.content
