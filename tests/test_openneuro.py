import os

import httpx
import pytest

from data_aggregator_mcp import openneuro

_DOI = "10.18112/openneuro.ds000001.v1.0.0"
_GQL = {
    "data": {
        "snapshot": {
            "files": [
                {
                    "filename": "README",
                    "size": 1175,
                    "directory": False,
                    "urls": [
                        "https://openneuro.org/crn/datasets/ds000001/objects/abc?filename=README"
                    ],
                },
                {"filename": "sub-01", "size": None, "directory": True, "urls": []},
                {
                    "filename": "dataset_description.json",
                    "size": 615,
                    "directory": False,
                    "urls": [
                        "https://openneuro.org/crn/datasets/ds000001/objects/def?filename=dataset_description.json"
                    ],
                },
            ]
        }
    }
}


@pytest.mark.asyncio
async def test_files_builds_manifest_from_doi():
    async def handler(request):
        assert request.url.host == "openneuro.org" and request.url.path.endswith("/graphql")
        body = request.content.decode()
        assert "ds000001" in body and "1.0.0" in body  # parsed from the DOI
        return httpx.Response(200, json=_GQL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await openneuro.files(c, _DOI)
    assert [f.name for f in files] == ["README", "dataset_description.json"]  # directory skipped
    assert files[0].url.startswith("https://openneuro.org/crn/datasets/ds000001/objects/")
    assert files[0].source == "openneuro" and files[0].size == 1175


@pytest.mark.asyncio
async def test_files_non_openneuro_doi_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"snapshot": None}})
        )
    ) as c:
        assert await openneuro.files(c, "10.5061/dryad.xyz") == []  # no parse → no network → []


@pytest.mark.asyncio
async def test_files_empty_snapshot_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"snapshot": None}})
        )
    ) as c:
        assert await openneuro.files(c, _DOI) == []


def test_wired_into_datacite_and_fetchable():
    from data_aggregator_mcp import datacite, server

    assert datacite._FILE_RESOLVERS.get("openneuro") is openneuro.files
    assert datacite._source_for_client("sul.openneuro") == "openneuro"
    assert "openneuro" in server._DATACITE_FETCHABLE


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_files_for_known_doi():
    async with httpx.AsyncClient(timeout=60) as c:
        fs = await openneuro.files(c, "10.18112/openneuro.ds000001.v1.0.0")
        assert fs and all(f.source == "openneuro" and f.url for f in fs)
