import os

import httpx
import pytest

from data_aggregator_mcp import uniprot
from data_aggregator_mcp.errors import NotFoundError

_ENTRY_INS = {
    "primaryAccession": "P01308",
    "uniProtkbId": "INS_HUMAN",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {"recommendedName": {"fullName": {"value": "Insulin"}}},
    "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
    "genes": [{"geneName": {"value": "INS"}}],
    "entryAudit": {"lastAnnotationUpdateDate": "2026-06-10"},
}
_ENTRY_TREMBL = {
    "primaryAccession": "A0A123",
    "uniProtkbId": "A0A123_ARATH",
    "entryType": "UniProtKB unreviewed (TrEMBL)",
    "proteinDescription": {"submissionNames": [{"fullName": {"value": "Uncharacterized protein"}}]},
    "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
}
_SEARCH = {"results": [_ENTRY_INS, _ENTRY_TREMBL]}


@pytest.mark.asyncio
async def test_search_reads_total_header_and_normalizes():
    def handler(request):
        assert "rest.uniprot.org" in request.url.host
        return httpx.Response(200, json=_SEARCH, headers={"x-total-results": "1887"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await uniprot.search(c, "insulin", size=2)
    assert total == 1887  # from the header, not the page length
    assert [r.id for r in recs] == ["uniprot:P01308", "uniprot:A0A123"]
    assert recs[0].title == "Insulin"
    assert recs[0].source == "uniprot" and recs[0].kind == "dataset"
    assert "Homo sapiens" in recs[0].subjects and "Swiss-Prot" in recs[0].subjects
    assert recs[0].identifiers.get("taxid") == "9606"
    assert recs[0].identifiers.get("gene") == "INS"
    # submissionNames fallback + TrEMBL curation label
    assert recs[1].title == "Uncharacterized protein"
    assert "Arabidopsis thaliana" in recs[1].subjects and "TrEMBL" in recs[1].subjects


@pytest.mark.asyncio
async def test_search_offset_returns_empty_without_network():
    def boom(request):
        raise AssertionError("cursor API: offset>0 must not hit the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        assert await uniprot.search(c, "insulin", size=10, offset=10) == (0, [])


@pytest.mark.asyncio
async def test_search_empty_returns_zero():
    def handler(request):
        return httpx.Response(200, json={"results": []}, headers={"x-total-results": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await uniprot.search(c, "zzzznohit", size=10) == (0, [])


@pytest.mark.asyncio
async def test_resolve_attaches_fasta_file():
    def handler(request):
        assert request.url.path.endswith("/P01308")
        return httpx.Response(200, json=_ENTRY_INS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await uniprot.resolve(c, "uniprot:p01308")  # lowercase -> upper-cased
    assert r.id == "uniprot:P01308" and r.title == "Insulin"
    assert len(r.files) == 1
    f = r.files[0]
    assert f.name == "P01308.fasta"
    assert f.url == "https://rest.uniprot.org/uniprotkb/P01308.fasta"
    assert f.mime == "text/x-fasta"


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    def handler(request):
        return httpx.Response(404, json={"messages": ["not found"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(NotFoundError):
            await uniprot.resolve(c, "uniprot:P00000000")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises_before_network():
    def boom(request):
        raise AssertionError("malformed id must fail on the guard, before the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(NotFoundError):
            await uniprot.resolve(c, "uniprot:")


@pytest.mark.asyncio
async def test_resolve_rejects_path_traversal_id_before_network():
    def boom(request):
        raise AssertionError("network must not be touched for an injection id")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(NotFoundError):
            await uniprot.resolve(c, "uniprot:P0/../secret")


def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "uniprot" in router.available_sources()
    assert router._ADAPTERS["uniprot"] is uniprot
    assert "uniprot:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "uniprot" for s in server._SOURCES)


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await uniprot.search(c, "insulin AND organism_id:9606", size=3)
        assert total > 0 and recs and recs[0].id.startswith("uniprot:")
        full = await uniprot.resolve(c, recs[0].id)
        assert any(f.name.endswith(".fasta") for f in full.files)
