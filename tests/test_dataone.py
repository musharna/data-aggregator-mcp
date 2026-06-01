import os

import httpx
import pytest

from data_aggregator_mcp import dataone
from data_aggregator_mcp.errors import NotFoundError
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
