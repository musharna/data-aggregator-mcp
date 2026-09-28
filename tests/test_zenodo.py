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


async def test_resolve_after_search_skips_the_redundant_get(httpx_mock: HTTPXMock) -> None:
    """E2: search stashes the full record, so resolve serves it from cache without a second
    GET. The resolve GET is deliberately NOT mocked — if resolve tried it, pytest-httpx would
    raise 'no response mocked'."""
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=phelipanche&size=10",
        json={"hits": {"total": 1, "hits": [_record()]}},
    )
    async with httpx.AsyncClient() as client:
        await zenodo.search(client, "phelipanche")  # seeds _SEARCH_CACHE
        r = await zenodo.resolve(client, "zenodo:7654321")
    # full record served from cache (files present, which the compact search view lacked)
    assert r.doi == "10.5281/zenodo.7654321" and len(r.files) == 1
    assert len(httpx_mock.get_requests()) == 1  # only the search hit the network


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


def test_normalize_maps_metrics_from_stats() -> None:
    rec = _record()
    rec["stats"] = {
        "views": 1234,
        "unique_views": 1000,
        "downloads": 56,
        "unique_downloads": 50,
    }
    r = zenodo._normalize(rec)
    assert r.metrics is not None
    assert r.metrics.views == 1234
    assert r.metrics.downloads == 56


def test_normalize_metrics_none_when_stats_absent() -> None:
    # _record() carries no "stats" key -> metrics stays None (not exposed != zero).
    assert zenodo._normalize(_record()).metrics is None


def test_normalize_metrics_coerces_floats_and_skips_nulls() -> None:
    rec = _record()
    rec["stats"] = {"views": 12.0, "downloads": None}
    r = zenodo._normalize(rec)
    assert r.metrics is not None
    assert r.metrics.views == 12
    assert r.metrics.downloads is None


def _versioned(recid: int, *, is_last: bool, index: int, related: list[dict]) -> dict:
    rec = _record()
    rec["id"] = recid
    rec["doi"] = f"10.5281/zenodo.{recid}"
    rec["conceptrecid"] = "13993786"
    rec["conceptdoi"] = "10.5281/zenodo.13993786"
    rec["metadata"]["related_identifiers"] = related
    rec["metadata"]["relations"] = {
        "version": [
            {
                "index": index,
                "is_last": is_last,
                "parent": {"pid_type": "recid", "pid_value": "13993786"},
            }
        ]
    }
    return rec


# Shape of live zenodo:13993787 (2026-09-27): NOT the last version of its concept, yet it
# carries isNewVersionOf -> another record, which the generic inference read as "latest".
_SUPERSEDED_RELATED = [
    {"relation": "isNewVersionOf", "identifier": "10.5281/zenodo.11099111", "scheme": "doi"}
]


def test_normalize_version_status_from_relations_version() -> None:
    old = zenodo._normalize(
        _versioned(13993787, is_last=False, index=0, related=_SUPERSEDED_RELATED)
    )
    assert old.is_latest is False
    assert old.superseded_by is None  # the newer id is not in the record; never invented
    new = zenodo._normalize(_versioned(13993788, is_last=True, index=1, related=[]))
    assert new.is_latest is True  # positive control: Zenodo's own is_last=true
    assert zenodo._normalize(_record()).is_latest is None  # no version graph -> unknown


async def test_resolve_superseded_version_is_not_latest_end_to_end(httpx_mock: HTTPXMock) -> None:
    """A-H1: router.resolve must report Zenodo's is_last, not the isNewVersionOf inference."""
    from data_aggregator_mcp import router

    httpx_mock.add_response(
        url="https://zenodo.org/api/records/13993787",
        json=_versioned(13993787, is_last=False, index=0, related=_SUPERSEDED_RELATED),
    )
    httpx_mock.add_response(
        url="https://zenodo.org/api/records/13993788",
        json=_versioned(13993788, is_last=True, index=1, related=_SUPERSEDED_RELATED),
    )
    httpx_mock.add_response(  # the non-latest record's one latest-version lookup
        method="HEAD",
        url="https://zenodo.org/api/records/13993787/versions/latest",
        status_code=301,
        headers={"Location": "https://zenodo.org/api/records/13993788"},
    )
    router._RESOLVE_CACHE.clear()
    zenodo._SEARCH_CACHE.clear()
    async with httpx.AsyncClient() as client:
        old = await router.resolve(client, "zenodo:13993787")
        new = await router.resolve(client, "zenodo:13993788")
    assert old.is_latest is False and old.superseded_by == "zenodo:13993788"
    assert new.is_latest is True and new.superseded_by is None  # positive control


_CONCEPT_OLD = 7421899  # live: v0.2.0 of concept 7421898 (is_last=false)
_CONCEPT_NEW = 10396807  # live: the last version of concept 7421898


def _concept_pair_mock(httpx_mock: HTTPXMock) -> None:
    for recid, is_last, index in ((_CONCEPT_OLD, False, 0), (_CONCEPT_NEW, True, 1)):
        httpx_mock.add_response(
            url=f"https://zenodo.org/api/records/{recid}",
            json=_versioned(recid, is_last=is_last, index=index, related=[]),
        )


async def test_resolve_superseded_by_from_latest_redirect_feeds_relate(
    httpx_mock: HTTPXMock,
) -> None:
    """X-M1: a non-latest record learns its newer version from ONE HEAD on
    /versions/latest (301 -> the latest record), so relate emits version_lineage.
    A latest record makes no extra call (the count is the positive control)."""
    from data_aggregator_mcp import relate

    _concept_pair_mock(httpx_mock)
    httpx_mock.add_response(
        method="HEAD",
        url=f"https://zenodo.org/api/records/{_CONCEPT_OLD}/versions/latest",
        status_code=301,
        headers={"Location": f"https://zenodo.org/api/records/{_CONCEPT_NEW}"},
    )
    zenodo._SEARCH_CACHE.clear()
    async with httpx.AsyncClient() as client:
        new = await zenodo.resolve(client, f"zenodo:{_CONCEPT_NEW}")
        assert len(httpx_mock.get_requests()) == 1  # latest: the GET only, zero extra calls
        old = await zenodo.resolve(client, f"zenodo:{_CONCEPT_OLD}")
    assert len(httpx_mock.get_requests()) == 3  # older: GET + one HEAD
    assert old.superseded_by == f"zenodo:{_CONCEPT_NEW}" and new.superseded_by is None
    hints = [h for h in relate.detect([new, old]) if h.kind == "version_lineage"]
    assert [h.resources for h in hints] == [[f"zenodo:{_CONCEPT_NEW}", f"zenodo:{_CONCEPT_OLD}"]]


async def test_resolve_latest_lookup_failure_is_enrichment_not_error(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """The /versions/latest HEAD is enrichment: an outage leaves superseded_by None and
    the resolve still succeeds with Zenodo's is_last."""
    from data_aggregator_mcp import _http

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    httpx_mock.add_response(
        url=f"https://zenodo.org/api/records/{_CONCEPT_OLD}",
        json=_versioned(_CONCEPT_OLD, is_last=False, index=0, related=[]),
    )
    httpx_mock.add_response(
        method="HEAD",
        url=f"https://zenodo.org/api/records/{_CONCEPT_OLD}/versions/latest",
        status_code=503,
        is_reusable=True,
    )
    zenodo._SEARCH_CACHE.clear()
    async with httpx.AsyncClient() as client:
        old = await zenodo.resolve(client, f"zenodo:{_CONCEPT_OLD}")
    assert old.is_latest is False and old.superseded_by is None
    assert any(r.method == "HEAD" for r in httpx_mock.get_requests())  # it was attempted


async def test_search_never_makes_the_latest_lookup(httpx_mock: HTTPXMock) -> None:
    """Search results carry is_latest from is_last but must not pay a HEAD per hit."""
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=iris&size=10",
        json={
            "hits": {
                "total": 1,
                "hits": [_versioned(_CONCEPT_OLD, is_last=False, index=0, related=[])],
            }
        },
    )
    async with httpx.AsyncClient() as client:
        _, results = await zenodo.search(client, "iris")
    assert results[0].is_latest is False and results[0].superseded_by is None
    assert [r.method for r in httpx_mock.get_requests()] == ["GET"]


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


@live_only
async def test_live_version_status_from_zenodo_relations() -> None:
    """A-H1/X-M1 live: 13993787 is not the last version of its concept (is_last=false);
    10396807 is the last version of concept 7421898 and 7421899 its older sibling."""
    async with httpx.AsyncClient() as client:
        superseded = await zenodo.resolve(client, "zenodo:13993787")
        latest = await zenodo.resolve(client, "zenodo:10396807")
        older = await zenodo.resolve(client, "zenodo:7421899")
    assert superseded.is_latest is False
    assert older.is_latest is False
    assert latest.is_latest is True


@live_only
async def test_live_superseded_by_and_relate_version_lineage() -> None:
    """X-M1 live: 7421899 is superseded by 10396807 (its concept's latest version)."""
    from data_aggregator_mcp import relate

    zenodo._SEARCH_CACHE.clear()
    async with httpx.AsyncClient() as client:
        old = await zenodo.resolve(client, "zenodo:7421899")
        new = await zenodo.resolve(client, "zenodo:10396807")
    assert old.superseded_by == "zenodo:10396807" and new.superseded_by is None
    assert any(h.kind == "version_lineage" for h in relate.detect([old, new]))
