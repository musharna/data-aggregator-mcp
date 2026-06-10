import os

import httpx
import pytest

from data_aggregator_mcp import cellxgene
from data_aggregator_mcp.errors import NotFoundError

# Trimmed REAL /curation/v1/collections shape (a bare JSON array).
_COLLECTIONS = [
    {
        "collection_id": "col-lung-1",
        "collection_url": "https://cellxgene.cziscience.com/collections/col-lung-1",
        "name": "Human lung cell atlas",
        "description": "An integrated atlas of the human respiratory system.",
        "doi": "10.1038/s41586-020-1111-1",
        "consortia": ["HCA"],
        "published_at": "2021-05-06T16:41:21+00:00",
        "revised_at": "2025-10-24T21:07:43+00:00",
        "visibility": "PUBLIC",
        "publisher_metadata": {
            "authors": [{"family": "Smith", "given": "Jane"}, {"name": "Lung Consortium"}],
            "journal": "Nature",
            "published_year": 2021,
            "is_preprint": False,
        },
        "links": [
            {
                "link_name": "GSE111",
                "link_type": "RAW_DATA",
                "link_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE111",
            },
        ],
        "datasets": [
            {
                "dataset_id": "ds-1",
                "tissue": [{"label": "lung", "ontology_term_id": "UBERON:0002048"}],
                "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
                "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
                "assay": [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}],
            },
        ],
    },
    {
        "collection_id": "col-brain-2",
        "collection_url": "https://cellxgene.cziscience.com/collections/col-brain-2",
        "name": "Mouse cortex survey",
        "description": "Single-cell survey of the mouse cortex.",
        "doi": None,  # 17/379 collections have no DOI
        "consortia": [],
        "published_at": "2022-01-02T00:00:00+00:00",
        "revised_at": "2022-02-02T00:00:00+00:00",
        "visibility": "PUBLIC",
        "publisher_metadata": {
            "authors": [{"family": "Doe", "given": "John"}],
            "published_year": 2022,
            "is_preprint": True,
        },
        "links": [],
        "datasets": [
            {
                "dataset_id": "ds-2",
                "tissue": [{"label": "cortex", "ontology_term_id": "UBERON:0000956"}],
                "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
                "organism": [{"label": "Mus musculus", "ontology_term_id": "NCBITaxon:10090"}],
                "assay": [{"label": "Smart-seq2", "ontology_term_id": "EFO:0008931"}],
            },
        ],
    },
]


@pytest.mark.asyncio
async def test_search_filters_collections_on_tissue_and_normalizes():
    async def handler(request):
        assert request.url.path.endswith("/curation/v1/collections")
        return httpx.Response(200, json=_COLLECTIONS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await cellxgene.search(c, "lung", size=10)
    assert total == 1  # only the lung collection's nested tissue matches
    r = recs[0]
    assert r.id == "cellxgene:col-lung-1" and r.source == "cellxgene" and r.kind == "dataset"
    assert r.title == "Human lung cell atlas" and r.year == 2021
    assert r.doi == "10.1038/s41586-020-1111-1"
    assert [cr.name for cr in r.creators] == ["Smith, Jane", "Lung Consortium"]
    assert "Homo sapiens" in r.organism and "lung" in r.subjects
    assert r.files == []  # listing carries no files
    # link mapping: landing_page + the RAW_DATA cross-ref to GEO
    assert any(lnk.rel == "landing_page" for lnk in r.links)
    assert any("geo" in lnk.target_id for lnk in r.links)


@pytest.mark.asyncio
async def test_search_all_terms_must_match_and_paginates():
    async def handler(request):
        return httpx.Response(200, json=_COLLECTIONS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # multi-term AND: "mouse cortex" matches only the brain collection
        total, recs = await cellxgene.search(c, "mouse cortex", size=10)
        assert total == 1 and recs[0].id == "cellxgene:col-brain-2"
        # offset past the single match → empty window, total still reflects full match count
        total2, recs2 = await cellxgene.search(c, "normal", size=1, offset=1)
        assert total2 == 2 and recs2[0].id == "cellxgene:col-brain-2"  # 2nd of 2 "normal" matches


@pytest.mark.asyncio
async def test_search_non_list_body_is_empty():
    # the default fan-out test mocks every source with `{}` (a dict, not a list);
    # search must coerce non-list bodies to [] instead of iterating dict keys.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        assert await cellxgene.search(c, "x") == (0, [])


_DETAIL = {
    "collection_id": "col-lung-1",
    "collection_url": "https://cellxgene.cziscience.com/collections/col-lung-1",
    "name": "Human lung cell atlas",
    "description": "An integrated atlas.",
    "doi": "10.1038/s41586-020-1111-1",
    "consortia": ["HCA"],
    "published_at": "2021-05-06T16:41:21+00:00",
    "revised_at": "2025-10-24T21:07:43+00:00",
    "publisher_metadata": {
        "authors": [{"family": "Smith", "given": "Jane"}],
        "published_year": 2021,
    },
    "links": [],
    "datasets": [
        {
            "dataset_id": "ds-1",
            "title": "Lung 10x",
            "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
            "tissue": [{"label": "lung", "ontology_term_id": "UBERON:0002048"}],
            "assets": [
                {
                    "filesize": 421889692,
                    "filetype": "H5AD",
                    "url": "https://datasets.cellxgene.cziscience.com/ds-1.h5ad",
                },
                {
                    "filesize": 511111111,
                    "filetype": "RDS",
                    "url": "https://datasets.cellxgene.cziscience.com/ds-1.rds",
                },
                {"filesize": 1, "filetype": "H5AD", "url": None},  # url-less → skipped
            ],
        },
        {
            "dataset_id": "ds-2",
            "title": "Lung Smart-seq",
            "assets": [
                {
                    "filesize": 222,
                    "filetype": "H5AD",
                    "url": "https://datasets.cellxgene.cziscience.com/ds-2.h5ad",
                }
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_resolve_flattens_assets_into_files():
    async def handler(request):
        assert request.url.path.endswith("/curation/v1/collections/col-lung-1")
        return httpx.Response(200, json=_DETAIL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await cellxgene.resolve(c, "cellxgene:col-lung-1")
    assert r.id == "cellxgene:col-lung-1" and r.doi == "10.1038/s41586-020-1111-1"
    assert [f.name for f in r.files] == ["Lung 10x.h5ad", "Lung 10x.rds", "Lung Smart-seq.h5ad"]
    assert r.files[0].url == "https://datasets.cellxgene.cziscience.com/ds-1.h5ad"
    assert r.files[0].size == 421889692 and r.files[0].source == "cellxgene"
    assert all(f.checksum is None for f in r.files)  # unverified — no digest in the API


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await cellxgene.resolve(c, "cellxgene:does-not-exist")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await cellxgene.resolve(c, "cellxgene:")


def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "cellxgene" in router.available_sources()
    assert router._ADAPTERS["cellxgene"] is cellxgene
    # native backend precedes datacite in merge precedence
    names = list(router._ADAPTERS)
    assert names.index("cellxgene") < names.index("datacite")
    assert "cellxgene:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "cellxgene" for s in server._SOURCES)


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=120) as c:
        total, recs = await cellxgene.search(c, "lung", size=3)
        assert total > 0 and recs and recs[0].id.startswith("cellxgene:")
        full = await cellxgene.resolve(c, recs[0].id)
        assert full.kind == "dataset" and full.files  # asset manifest attached
        assert all(f.url and f.url.startswith("https://datasets.cellxgene") for f in full.files)
