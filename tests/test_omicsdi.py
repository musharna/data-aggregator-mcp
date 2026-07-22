import os

import httpx
import pytest

from data_aggregator_mcp import omicsdi
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry

_SEARCH = {
    "count": 740517,
    "datasets": [
        {
            "id": "MTBLS1355",
            "source": "metabolights_dataset",
            "title": "Breast Cancer Metabolomics",
            "description": "A metabolomics study.",
        },
        {
            "id": "PXD000001",
            "source": "pride",
            "title": "TMT spike-in",
            "description": "Proteomics.",
        },
        {
            "id": "GSE12345",
            "source": "omics_geo",
            "title": "Some transcriptomics",
            "description": "RNA.",
        },
    ],
}


@pytest.mark.asyncio
async def test_search_keeps_only_modality_repos():
    async def handler(request):
        assert request.url.path.endswith("/dataset/search")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await omicsdi.search(c, "cancer", size=10)
    ids = [r.id for r in recs]
    assert ids == ["omicsdi:metabolights_dataset:MTBLS1355", "omicsdi:pride:PXD000001"]
    assert total == 2  # GEO hit dropped; total is the kept count
    assert recs[0].source == "omicsdi" and recs[0].kind == "study"
    assert recs[0].files == []


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        assert await omicsdi.search(c, "x", size=10, offset=10) == (0, [])


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_modality_only():
    async with httpx.AsyncClient(timeout=60) as c:
        _total, recs = await omicsdi.search(c, "cancer", size=20)
        assert all(r.id.split(":")[1] in omicsdi._MODALITY_REPOS for r in recs)


_RECORD = {"accession": "PXD000001", "name": "TMT spike-in", "description": "Proteomics study."}


@pytest.mark.asyncio
async def test_resolve_pride_routes_to_pride_files(monkeypatch):
    async def fake_pride_files(client, acc):
        assert acc == "PXD000001"
        return [FileEntry(name="a.raw", url="https://ftp.pride.ebi.ac.uk/a.raw", source="pride")]

    monkeypatch.setattr("data_aggregator_mcp.pride.files", fake_pride_files)

    async def handler(request):
        assert request.url.path.endswith("/dataset/pride/PXD000001")
        return httpx.Response(200, json=_RECORD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD000001")
    assert r.id == "omicsdi:pride:PXD000001" and r.title == "TMT spike-in"
    assert [f.name for f in r.files] == ["a.raw"]
    assert any(lnk.rel == "landing_page" for lnk in r.links)


@pytest.mark.asyncio
async def test_resolve_non_fetchable_repo_has_empty_files():
    rec = {"accession": "PXD9", "name": "x", "description": "y"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rec))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:massive:MSV000001")
    assert r.files == []  # MassIVE is discovery-only this wave


_RECORD_PRIDE_RICH = {
    "accession": "PXD002213",
    "name": "Gastric cancer ascites proteome",
    "description": "Comparative proteomics.",
    "additional": {
        "submitter": ["Dohyun Han"],
        "species": ["Homo Sapiens (human)"],
        "publication": ["29654727 Jin J, Son M. Comparative proteomic analysis."],
    },
}

_RECORD_MTBLS_RICH = {
    "accession": "MTBLS9830",
    "name": "Medulloblastoma metabolome",
    "description": "Multiomic profiling.",
    "additional": {
        "submitter_name": ["Jane Roe"],
        "author": ["Jane Roe", "John Doe"],  # present but NOT a creator source
        "organism": ["Homo sapiens"],
        "publication": ["Multiomic profiling of medulloblastoma. 10.1101/2023.01.09.523234."],
    },
}


@pytest.mark.asyncio
async def test_resolve_enriches_pride_provenance(monkeypatch):
    """D3: PRIDE additional → creators (submitter), organism (species, verbatim),
    pmid from a leading-digit publication token."""
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD_PRIDE_RICH))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD002213")
    assert [cr.name for cr in r.creators] == ["Dohyun Han"]
    assert r.organism == ["Homo Sapiens (human)"]
    assert r.identifiers.get("pmid") == "29654727"


@pytest.mark.asyncio
async def test_resolve_enriches_metabolights_doi_and_submitter_fallback():
    """D3: a repo using `submitter_name`/`organism` keys still yields creators (the
    depositor, NOT the paper `author` list), organism, and a DOI from publication."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD_MTBLS_RICH))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:metabolights_dataset:MTBLS9830")
    assert [cr.name for cr in r.creators] == ["Jane Roe"]  # submitter_name, not author
    assert r.organism == ["Homo sapiens"]
    assert r.doi == "10.1101/2023.01.09.523234"
    assert r.identifiers.get("pmid") is None  # no leading-digit token


@pytest.mark.asyncio
async def test_resolve_sparse_record_stays_clean(monkeypatch):
    """A record with no `additional` block adds no fabricated creators/organism."""
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD000001")
    assert r.creators == [] and r.organism == [] and r.identifiers == {}


async def _aempty():
    return []


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await omicsdi.resolve(c, "omicsdi:onlytwo")
