from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import datacite
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator


def _item() -> dict:
    return {
        "id": "10.5061/dryad.98sf7m0wt",
        "type": "dois",
        "attributes": {
            "doi": "10.5061/dryad.98sf7m0wt",
            "titles": [{"title": "Data and code from: retinal study"}],
            "creators": [{"name": "Doe, J."}, {"name": "Roe, R."}],
            "publicationYear": 2023,
            "descriptions": [{"description": "Multi-omics dataset."}],
            "subjects": [{"subject": "retina"}, {"subject": "omics"}],
            "rightsList": [{"rights": "CC0 1.0", "rightsIdentifier": "cc0-1.0"}],
            "types": {"resourceTypeGeneral": "Dataset"},
            "url": "https://datadryad.org/dataset/doi:10.5061/dryad.98sf7m0wt",
            "publisher": "Dryad",
        },
        "relationships": {"client": {"data": {"id": "dryad.dryad", "type": "clients"}}},
    }


def test_normalize_maps_core_fields() -> None:
    r = datacite._normalize(_item())
    assert r.id == "datacite:10.5061/dryad.98sf7m0wt"
    assert r.source == "dryad"
    assert r.kind == "dataset"
    assert r.title == "Data and code from: retinal study"
    assert r.creators == [Creator(name="Doe, J."), Creator(name="Roe, R.")]
    assert r.year == 2023
    assert r.doi == "10.5061/dryad.98sf7m0wt"
    assert r.license == "cc0-1.0"
    assert r.subjects == ["retina", "omics"]
    assert r.files == []


def test_source_derives_from_client_id_not_publisher() -> None:
    item = _item()
    item["relationships"]["client"]["data"]["id"] = "figshare.ars"
    item["attributes"]["publisher"] = "Taylor & Francis"
    assert datacite._normalize(item).source == "figshare"


def test_kind_maps_journal_article_to_publication() -> None:
    item = _item()
    item["attributes"]["types"]["resourceTypeGeneral"] = "JournalArticle"
    assert datacite._normalize(item).kind == "publication"


def test_unknown_client_falls_back_to_raw_id() -> None:
    item = _item()
    item["relationships"]["client"]["data"]["id"] = "tib.foobar"
    assert datacite._normalize(item).source == "tib.foobar"


def test_source_for_client_maps_gdcc_to_dataverse() -> None:
    from data_aggregator_mcp.datacite import _source_for_client

    assert _source_for_client("gdcc.harvard-dv") == "dataverse"
    assert _source_for_client("dans.dataversenl") == "dataverse"  # substring rule still works


async def test_search_returns_total_and_compact_resources(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=arabidopsis&page%5Bsize%5D=10",
        json={"data": [_item()], "meta": {"total": 1}},
    )
    async with httpx.AsyncClient() as client:
        total, results = await datacite.search(client, "arabidopsis")
    assert total == 1
    assert results[0].id == "datacite:10.5061/dryad.98sf7m0wt"
    assert results[0].source == "dryad"
    assert results[0].files == []


async def test_resolve_strips_prefix_and_returns_full_record(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.datacite.org/dois/10.5061/dryad.98sf7m0wt",
        json={"data": _item()},
    )
    # resolve now fans out to the Dryad manifest resolver; an empty version link
    # means dryad.files returns [] (no second call) and leaves files=[].
    httpx_mock.add_response(
        url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.98sf7m0wt",
        json={"_links": {}},
    )
    async with httpx.AsyncClient() as client:
        r = await datacite.resolve(client, "datacite:10.5061/dryad.98sf7m0wt")
    assert r.doi == "10.5061/dryad.98sf7m0wt"
    assert r.source == "dryad"
    assert r.files == []


async def test_resolve_404_raises_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.datacite.org/dois/10.9999/nope", status_code=404)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no DOI"):
            await datacite.resolve(client, "10.9999/nope")


async def test_resolve_attaches_osf_files(httpx_mock: HTTPXMock) -> None:
    # DataCite metadata (source=osf via client cos.osf) + OSF file listing.
    httpx_mock.add_response(
        url="https://api.datacite.org/dois/10.17605/osf.io/5pfej",
        json={
            "data": {
                "id": "10.17605/osf.io/5pfej",
                "attributes": {
                    "doi": "10.17605/osf.io/5pfej",
                    "titles": [{"title": "t"}],
                    "types": {"resourceTypeGeneral": "Dataset"},
                },
                "relationships": {"client": {"data": {"id": "cos.osf"}}},
            }
        },
    )
    httpx_mock.add_response(
        url="https://api.osf.io/v2/nodes/5pfej/files/osfstorage/",
        json={
            "data": [
                {
                    "attributes": {
                        "kind": "file",
                        "name": "a.csv",
                        "size": 179,
                        "extra": {"hashes": {"md5": "m"}},
                    },
                    "links": {"download": "https://osf.io/download/f1/"},
                }
            ],
            "links": {"next": None},
        },
    )
    async with httpx.AsyncClient() as client:
        r = await datacite.resolve(client, "datacite:10.17605/osf.io/5pfej")
    assert r.source == "osf"
    assert {f.name for f in r.files} == {"a.csv"}


def test_normalize_sets_access_open_for_open_license() -> None:
    item = {
        "attributes": {
            "doi": "10.5061/dryad.x",
            "titles": [{"title": "t"}],
            "types": {"resourceTypeGeneral": "Dataset"},
            "rightsList": [
                {
                    "rights": "Creative Commons Zero v1.0 Universal",
                    "rightsUri": "https://creativecommons.org/publicdomain/zero/1.0/legalcode",
                    "rightsIdentifier": "cc0-1.0",
                }
            ],
        },
        "relationships": {"client": {"data": {"id": "dryad.dryad"}}},
    }
    r = datacite._normalize(item)
    assert r.access == "open"
    assert r.license == "cc0-1.0"


def test_normalize_access_none_without_open_license() -> None:
    item = {
        "attributes": {
            "doi": "10.x/y",
            "titles": [{"title": "t"}],
            "types": {"resourceTypeGeneral": "Dataset"},
            "rightsList": [],
        },
        "relationships": {"client": {"data": {"id": "figshare.ars"}}},
    }
    assert datacite._normalize(item).access is None


@pytest.mark.asyncio
async def test_search_offset_requests_page_number_and_slices():
    captured = {}

    def rec(i):
        return {
            "id": f"10.x/{i}",
            "type": "dois",
            "attributes": {
                "doi": f"10.x/{i}",
                "titles": [{"title": f"t{i}"}],
                "publicationYear": 2020,
                "types": {},
            },
        }

    async def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(
            200, json={"data": [rec(i) for i in range(10)], "meta": {"total": 100}}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        total, recs = await datacite.search(client, "q", size=10, offset=20)
    assert captured["page[number]"] == "3"  # 20//10 + 1
    assert captured["page[size]"] == "10"
    assert len(recs) == 10  # 20%10 == 0, no slice


def test_normalize_extracts_creator_orcid() -> None:
    item = {
        "attributes": {
            "doi": "10.x/y",
            "titles": [{"title": "t"}],
            "types": {},
            "creators": [
                {
                    "name": "A",
                    "nameIdentifiers": [
                        {
                            "nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
                            "nameIdentifierScheme": "ORCID",
                        }
                    ],
                },
                {"name": "B"},
            ],
        }
    }
    r = datacite._normalize(item)
    assert r.creators[0].orcid == "0000-0002-1825-0097"
    assert r.creators[1].orcid is None


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_search_spans_multiple_repos() -> None:
    async with httpx.AsyncClient() as client:
        total, results = await datacite.search(client, "arabidopsis genome", size=5)
    assert total >= 1
    assert results and all(r.id.startswith("datacite:") for r in results)
    assert all(r.doi for r in results)


@live_only
async def test_live_resolve_dryad_doi() -> None:
    async with httpx.AsyncClient() as client:
        r = await datacite.resolve(client, "10.5061/dryad.98sf7m0wt")
    assert r.doi == "10.5061/dryad.98sf7m0wt"
    assert r.source == "dryad"


@live_only
async def test_live_resolve_dataverse_doi_attaches_files() -> None:
    # Harvard Dataverse DOI (gdcc.harvard-dv → source="dataverse"); resolve must
    # fan out to the Dataverse manifest resolver and populate files[].
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await datacite.resolve(client, "datacite:10.7910/DVN/TJCLKP")
    assert r.source == "dataverse"
    assert r.files


async def test_resolve_zenodo_doi_delegates_to_zenodo(httpx_mock: HTTPXMock) -> None:
    from data_aggregator_mcp import datacite

    # DataCite resolve → a Zenodo-client record (no files in DataCite metadata).
    httpx_mock.add_response(
        url="https://api.datacite.org/dois/10.5281/zenodo.7654321",
        json={
            "data": {
                "attributes": {
                    "doi": "10.5281/zenodo.7654321",
                    "titles": [{"title": "Z"}],
                    "types": {"resourceTypeGeneral": "Dataset"},
                },
                "relationships": {"client": {"data": {"id": "cern.zenodo"}}},
            }
        },
    )
    # Native Zenodo resolve → the file manifest.
    httpx_mock.add_response(
        url="https://zenodo.org/api/records/7654321",
        json={
            "id": 7654321,
            "doi": "10.5281/zenodo.7654321",
            "metadata": {"title": "Z"},
            "files": [
                {
                    "key": "data.csv",
                    "size": 10,
                    "links": {"self": "https://zenodo.org/.../data.csv/content"},
                    "checksum": "md5:abc",
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        r = await datacite.resolve(client, "datacite:10.5281/zenodo.7654321")
    assert r.source == "zenodo"
    assert [f.name for f in r.files] == ["data.csv"]
