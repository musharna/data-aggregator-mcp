import os

import httpx
import pytest

from data_aggregator_mcp import dataone
from data_aggregator_mcp.errors import DataAggregatorError, NotFoundError
from data_aggregator_mcp.models import Creator

_SEARCH = {
    "response": {
        "numFound": 17069,
        "docs": [
            {
                "identifier": "doi:10.18739/A26336",
                "title": "Soil Probes",
                "origin": ["Jane Doe", "John Roe"],
                "datePublished": "2015-06-01T00:00:00Z",
                "dateUploaded": "2015-05-01T00:00:00Z",
                "dateModified": "2016-01-01T00:00:00Z",
                "resourceMap": ["resource_map_doi:10.18739/A26336"],
            },
            {
                "identifier": "knb-lter-jrn.20050360.9823",
                "title": "Soil Nematodes",
                "author": "John Anderson",
                "dateUploaded": "2011-12-03T00:00:00Z",
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_search_normalizes_and_prefixes_id():
    async def handler(request):
        assert request.url.path.endswith("/query/solr/")
        assert "formatType:METADATA" in request.url.params["q"]
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dataone.search(c, "soil", size=10)
    assert total == 17069
    r0 = recs[0]
    assert r0.id == "dataone:doi:10.18739/A26336" and r0.source == "dataone"
    assert r0.kind == "dataset" and r0.year == 2015
    assert r0.creators == [Creator(name="Jane Doe"), Creator(name="John Roe")]
    assert r0.last_updated == "2016-01-01T00:00:00Z"
    assert r0.files == []  # _normalize yields no files; resolve populates them
    # single-author fallback when origin absent
    assert recs[1].creators == [Creator(name="John Anderson")] and recs[1].year == 2011


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await dataone.search(c, "soil", size=5)
        assert total > 0 and recs
        assert recs[0].id.startswith("dataone:") and recs[0].source == "dataone"


_META_DOC = {
    "response": {
        "numFound": 1,
        "docs": [
            {
                "identifier": "doi:10.18739/A26336",
                "title": "Soil Probes",
                "origin": ["Jane Doe"],
                "datePublished": "2015-06-01T00:00:00Z",
                "resourceMap": ["resource_map_doi:10.18739/A26336"],
            }
        ],
    }
}

_DATA_DOCS = {
    "response": {
        "numFound": 1,
        "docs": [
            {
                "identifier": "urn:uuid:e2919f95-81e2-4aec-a6f0-b46861c1822b",
                "fileName": "Probes2011.xlsx",
                "size": 28270,
                "checksum": "3640858c8d60658422b619ea34f5b1afc1be4903ea3948ed68261cbea76e11d0",
                "checksumAlgorithm": "SHA256",
            }
        ],
    }
}

_OBJLOC = (
    '<?xml version="1.0"?><ns2:objectLocationList '
    'xmlns:ns2="http://ns.dataone.org/service/types/v1">'
    "<objectLocation><url>https://arcticdata.io/metacat/d1/mn/v2/object/"
    "urn:uuid:e2919f95-81e2-4aec-a6f0-b46861c1822b</url></objectLocation>"
    "</ns2:objectLocationList>"
)


@pytest.mark.asyncio
async def test_resolve_attaches_data_files_with_checksum():
    def handler(request):
        p, q = request.url.path, request.url.params.get("q", "")
        if p.endswith("/query/solr/") and "identifier:" in q:
            return httpx.Response(200, json=_META_DOC)
        if p.endswith("/query/solr/") and "formatType:DATA" in q:
            return httpx.Response(200, json=_DATA_DOCS)
        if "/resolve/" in p:
            return httpx.Response(200, text=_OBJLOC)
        raise AssertionError(f"unexpected request {p}?{q}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await dataone.resolve(c, "dataone:doi:10.18739/A26336")
    assert len(r.files) == 1
    f = r.files[0]
    assert f.name == "Probes2011.xlsx" and f.size == 28270
    assert f.checksum == ("sha256:3640858c8d60658422b619ea34f5b1afc1be4903ea3948ed68261cbea76e11d0")
    assert f.url.startswith("https://arcticdata.io/")


@pytest.mark.asyncio
async def test_resolve_no_resource_map_returns_empty_files():
    doc = {"response": {"numFound": 1, "docs": [{"identifier": "x", "title": "t"}]}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=doc))
    ) as c:
        r = await dataone.resolve(c, "dataone:x")
    assert r.files == []


@pytest.mark.asyncio
async def test_resolve_404_when_no_doc():
    empty = {"response": {"numFound": 0, "docs": []}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=empty))
    ) as c:
        with pytest.raises(NotFoundError):
            await dataone.resolve(c, "dataone:missing")


@pytest.mark.asyncio
async def test_resolve_normalizes_hyphenated_checksum_algorithm():
    meta = {
        "response": {
            "numFound": 1,
            "docs": [
                {
                    "identifier": "doi:10.5/x",
                    "title": "t",
                    "resourceMap": ["resource_map_doi:10.5/x"],
                }
            ],
        }
    }
    data = {
        "response": {
            "numFound": 1,
            "docs": [
                {
                    "identifier": "urn:uuid:abc",
                    "fileName": "f.csv",
                    "size": 5,
                    "checksum": "deadbeef",
                    "checksumAlgorithm": "SHA-256",
                }
            ],
        }
    }
    objloc = (
        '<ns2:objectLocationList xmlns:ns2="http://ns.dataone.org/service/types/v1">'
        "<objectLocation><url>https://mn.example/object/urn:uuid:abc</url>"
        "</objectLocation></ns2:objectLocationList>"
    )

    def handler(request):
        p, q = request.url.path, request.url.params.get("q", "")
        if p.endswith("/query/solr/") and "identifier:" in q:
            return httpx.Response(200, json=meta)
        if p.endswith("/query/solr/") and "formatType:DATA" in q:
            return httpx.Response(200, json=data)
        if "/resolve/" in p:
            return httpx.Response(200, text=objloc)
        raise AssertionError(f"unexpected {p}?{q}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await dataone.resolve(c, "dataone:doi:10.5/x")
    assert r.files[0].checksum == "sha256:deadbeef"  # hyphen stripped → valid hashlib name


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_and_fetch_verifies_checksum():
    from data_aggregator_mcp import fetch

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        r = await dataone.resolve(c, "dataone:doi:10.18739/A26336")
        assert r.files and r.files[0].checksum  # a verified file is present
        small = min(r.files, key=lambda f: f.size or (1 << 62))
        dest = os.environ.get("CLAUDE_JOB_DIR", "/tmp") + "/d1test"
        res = await fetch.fetch_files(c, r.model_copy(update={"files": [small]}), dest=dest)
        assert res.paths  # fetch.py raises on checksum mismatch, so reaching here = verified


# ---------------------------------------------------------------------------
# Fix #1 — _object_url must fail loud on 5xx (route through _http.request_xml)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_url_5xx_raises_dataaggreagtor_error():
    """A persistent 5xx from /cn/v2/resolve/ must raise, never silently return None."""
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if "/resolve/" in request.url.path:
            return httpx.Response(500, text="internal error")
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(DataAggregatorError):
            await dataone._object_url(c, "urn:uuid:test-5xx")
    # must have retried (MAX_RETRIES=3)
    assert call_count == dataone.MAX_RETRIES


@pytest.mark.asyncio
async def test_object_url_404_returns_none():
    """A 404 from /cn/v2/resolve/ means object not locatable → return None (skip it)."""

    def handler(request):
        if "/resolve/" in request.url.path:
            return httpx.Response(404, text="not found")
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        result = await dataone._object_url(c, "urn:uuid:test-404")
    assert result is None


@pytest.mark.asyncio
async def test_object_url_303_returns_location_without_following():
    """CN /resolve/ answers 303 with the Member-Node url in Location; the redirect
    must NOT be followed (else we download the object bytes instead of the locator)."""
    mn_url = "https://arcticdata.io/metacat/d1/mn/v2/object/urn:uuid:abc"

    def handler(request):
        if "/resolve/" in request.url.path:
            return httpx.Response(303, headers={"Location": mn_url}, text="<redirect/>")
        # reaching here means the 303 was followed to the object bytes — the bug.
        raise AssertionError(f"redirect was followed to {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as c:
        result = await dataone._object_url(c, "urn:uuid:abc")
    assert result == mn_url


# ---------------------------------------------------------------------------
# Fix #3 — _normalize must populate doi from doi:-prefixed PIDs
# ---------------------------------------------------------------------------


def test_normalize_sets_doi_from_doi_prefixed_pid():
    r = dataone._normalize({"identifier": "doi:10.18739/A26336", "title": "X"})
    assert r.doi == "10.18739/A26336"


def test_normalize_doi_none_for_uuid_pid():
    r = dataone._normalize({"identifier": "urn:uuid:abc", "title": "Y"})
    assert r.doi is None


def test_normalize_doi_none_for_plain_ark():
    r = dataone._normalize({"identifier": "knb-lter-jrn.20050360.9823", "title": "Z"})
    assert r.doi is None
