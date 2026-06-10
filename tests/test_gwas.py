import os

import httpx
import pytest

from data_aggregator_mcp import gwas
from data_aggregator_mcp.errors import NotFoundError

_SEARCH = {
    "_embedded": {
        "studies": [
            {
                "accessionId": "GCST000910",
                "diseaseTrait": {"trait": "Asthma"},
                "publicationInfo": {
                    "title": "Association near ORMDL3",
                    "publicationDate": "2010-12-08",
                    "pubmedId": 21150878,
                    "author": {"fullname": "Moffatt MF"},
                },
                "fullPvalueSet": True,
                "initialSampleSize": "10,365 cases",
                "snpCount": 561466,
            },
            {
                "accessionId": "GCST000576",
                "diseaseTrait": {"trait": "Asthma"},
                "publicationInfo": {
                    "title": "GWAS of asthma",
                    "publicationDate": "2010-02-01",
                    "pubmedId": 20159242,
                    "author": {"fullname": "Li X"},
                },
                "fullPvalueSet": False,
                "snpCount": 0,
            },
        ]
    },
    "page": {"size": 2, "totalElements": 87, "totalPages": 44, "number": 0},
}


@pytest.mark.asyncio
async def test_search_normalizes_studies():
    async def handler(request):
        assert request.url.path.endswith("/studies/search/findByDiseaseTrait")
        assert request.url.params.get("diseaseTrait") == "asthma"
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await gwas.search(c, "asthma", size=2)
    assert total == 87
    assert [r.id for r in recs] == ["gwas:GCST000910", "gwas:GCST000576"]
    r0 = recs[0]
    assert r0.source == "gwas" and r0.kind == "study"
    assert r0.title == "Association near ORMDL3" and r0.year == 2010
    assert r0.identifiers.get("pmid") == "21150878"
    assert "Asthma" in r0.subjects


@pytest.mark.asyncio
async def test_search_null_total_falls_back_to_page_length():
    payload = {
        "_embedded": {"studies": _SEARCH["_embedded"]["studies"]},
        "page": {"totalElements": None},
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as c:
        total, recs = await gwas.search(c, "asthma", size=2)
    assert total == 2 and len(recs) == 2  # int, not None


@pytest.mark.asyncio
async def test_search_paginates_by_page_number():
    captured = {}

    async def handler(request):
        captured["page"] = request.url.params.get("page")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await gwas.search(c, "asthma", size=10, offset=20)
    assert captured["page"] == "2"


_RECORD = {
    "accessionId": "GCST000028",
    "diseaseTrait": {"trait": "Type 2 diabetes"},
    "publicationInfo": {
        "title": "GWAS identifies loci for T2D",
        "publicationDate": "2007-06-01",
        "pubmedId": 17463249,
        "author": {"fullname": "Sladek R"},
    },
    "fullPvalueSet": True,
    "initialSampleSize": "1,924 cases",
    "snpCount": 392935,
}


@pytest.mark.asyncio
async def test_resolve_normalizes_study():
    async def handler(request):
        assert request.url.path.endswith("/studies/GCST000028")
        return httpx.Response(200, json=_RECORD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r.id == "gwas:GCST000028" and r.kind == "study"
    assert r.title == "GWAS identifies loci for T2D" and r.year == 2007
    assert r.identifiers.get("pmid") == "17463249"
    assert "Type 2 diabetes" in r.subjects and r.files == []


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
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
