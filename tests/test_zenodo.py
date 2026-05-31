from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import zenodo
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, FundingRef


def _record() -> dict:
    return {
        "id": 7654321,
        "doi": "10.5281/zenodo.7654321",
        "metadata": {
            "title": "Phelipanche small RNA dataset",
            "creators": [{"name": "Zangishei, Z."}, {"name": "Aubry, S."}],
            "publication_date": "2022-06-01",
            "description": "<p>sRNA reads</p>",
            "resource_type": {"type": "dataset"},
            "license": {"id": "cc-by-4.0"},
            "keywords": ["small RNA", "parasitic plant"],
        },
        "files": [
            {
                "key": "reads.fastq.gz",
                "size": 12345,
                "checksum": "md5:0123abc",
                "links": {
                    "self": "https://zenodo.org/api/records/7654321/files/reads.fastq.gz/content"
                },
            }
        ],
    }


def test_normalize_maps_core_fields() -> None:
    r = zenodo._normalize(_record())
    assert r.id == "zenodo:7654321"
    assert r.source == "zenodo"
    assert r.kind == "dataset"
    assert r.title == "Phelipanche small RNA dataset"
    assert r.creators == [Creator(name="Zangishei, Z."), Creator(name="Aubry, S.")]
    assert r.year == 2022
    assert r.doi == "10.5281/zenodo.7654321"
    assert r.license == "cc-by-4.0"
    assert r.subjects == ["small RNA", "parasitic plant"]


def test_normalize_maps_files_with_checksum() -> None:
    r = zenodo._normalize(_record())
    assert len(r.files) == 1
    f = r.files[0]
    assert f.name == "reads.fastq.gz"
    assert f.size == 12345
    assert f.checksum == "md5:0123abc"
    assert f.url.endswith("/reads.fastq.gz/content")


async def test_search_returns_total_and_resources(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=phelipanche&size=10",
        json={"hits": {"total": 1, "hits": [_record()]}},
    )
    async with httpx.AsyncClient() as client:
        total, results = await zenodo.search(client, "phelipanche")
    assert total == 1
    assert results[0].id == "zenodo:7654321"
    # Search results are COMPACT (token-budget premise): no file manifest,
    # description truncated. Full record + files come from resolve.
    assert results[0].files == []


async def test_resolve_strips_prefix_and_normalizes(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://zenodo.org/api/records/7654321",
        json=_record(),
    )
    async with httpx.AsyncClient() as client:
        r = await zenodo.resolve(client, "zenodo:7654321")
    assert r.doi == "10.5281/zenodo.7654321"


async def test_resolve_404_raises_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://zenodo.org/api/records/99", status_code=404)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no record id='99'"):
            await zenodo.resolve(client, "99")


def test_normalize_sets_access_from_access_right() -> None:
    from data_aggregator_mcp import zenodo

    rec = {
        "id": 7,
        "doi": "10.5281/zenodo.7",
        "metadata": {
            "title": "t",
            "resource_type": {"type": "dataset"},
            "access_right": "embargoed",
            "license": {"id": "cc-by-4.0"},
        },
    }
    r = zenodo._normalize(rec)
    assert r.access == "embargoed"
    assert r.license == "cc-by-4.0"


def test_normalize_access_none_when_absent() -> None:
    from data_aggregator_mcp import zenodo

    rec = {"id": 8, "metadata": {"title": "t", "resource_type": {"type": "dataset"}}}
    assert zenodo._normalize(rec).access is None


@pytest.mark.asyncio
async def test_search_offset_requests_page_and_slices():
    captured = {}

    def make_record(i):
        return {
            "id": i,
            "metadata": {"title": f"r{i}", "publication_date": "2020-01-01"},
            "files": [],
        }

    async def handler(request):
        captured.update(dict(request.url.params))
        recs = [make_record(i) for i in range(10)]
        return httpx.Response(200, json={"hits": {"total": 100, "hits": recs}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # offset=13, size=10 -> page 2 (offset//size+1), slice [13%10:] = drop first 3
        total, recs = await zenodo.search(client, "q", size=10, offset=13)
    assert captured["page"] == "2"
    assert captured["size"] == "10"
    assert len(recs) == 7  # 10 returned, sliced off first 3


@pytest.mark.asyncio
async def test_search_offset_zero_unchanged():
    captured = {}

    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": {"total": 0, "hits": []}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await zenodo.search(client, "q", size=10)
    assert captured.get("page", "1") == "1"


def test_normalize_extracts_creator_orcid() -> None:
    rec = {
        "id": 1,
        "metadata": {
            "title": "t",
            "creators": [
                {"name": "A", "orcid": "0000-0002-1825-0097"},
                {"name": "B"},
            ],
        },
    }
    r = zenodo._normalize(rec)
    assert r.creators[0].orcid == "0000-0002-1825-0097"
    assert r.creators[1].orcid is None


def test_normalize_extracts_funding() -> None:
    rec = {
        "id": 1,
        "metadata": {
            "title": "t",
            "grants": [
                {"code": "654321", "funder": {"name": "European Commission"}},
                {"title": "T", "funder": {"name": "NSF"}},
                {"code": "x"},
            ],
        },
    }
    r = zenodo._normalize(rec)
    assert r.funding == [
        FundingRef(funder="European Commission", award="654321"),
        FundingRef(funder="NSF", award="T"),
    ]


def test_normalize_extracts_related_links() -> None:
    rec = {
        "id": 1,
        "metadata": {
            "title": "t",
            "related_identifiers": [{"identifier": "10.1/x", "relation": "isPartOf"}],
        },
    }
    r = zenodo._normalize(rec)
    assert ("is_part_of", "10.1/x") in {(link.rel, link.target_id) for link in r.links}


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_search_returns_hits() -> None:
    async with httpx.AsyncClient() as client:
        total, results = await zenodo.search(client, "arabidopsis RNA-seq", size=3)
    assert total >= 1
    assert results and results[0].id.startswith("zenodo:")
    assert results[0].title


@live_only
async def test_live_resolve_known_record_has_files() -> None:
    async with httpx.AsyncClient() as client:
        total, results = await zenodo.search(client, "arabidopsis", size=1)
        resolved = await zenodo.resolve(client, results[0].id)
    assert resolved.id == results[0].id
    assert resolved.doi
