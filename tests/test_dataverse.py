from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import dataverse

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_DATASET = {
    "data": {
        "latestVersion": {
            "versionState": "RELEASED",
            "files": [
                {
                    "label": "language.py",
                    "restricted": False,
                    "dataFile": {
                        "id": 4202258,
                        "filename": "language.py",
                        "filesize": 590,
                        "md5": "d0763edaa9d9bd2a9516280e9044d885",
                    },
                },
                {
                    "label": "secret.csv",
                    "restricted": True,
                    "dataFile": {"id": 999, "filename": "secret.csv", "filesize": 10, "md5": "x"},
                },
            ],
        }
    }
}


async def test_files_parses_dataset_skips_restricted(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/TJCLKP",
        json=_DATASET,
    )
    async with httpx.AsyncClient() as client:
        files = await dataverse.files(client, "10.7910/DVN/TJCLKP")
    assert {f.name for f in files} == {"language.py"}  # restricted dropped
    f = files[0]
    assert f.url == "https://dataverse.harvard.edu/api/access/datafile/4202258"
    assert f.size == 590
    assert f.checksum == "md5:d0763edaa9d9bd2a9516280e9044d885"


async def test_base_url_env_override(httpx_mock, monkeypatch) -> None:
    monkeypatch.setenv("DATAVERSE_BASE_URL", "https://darus.uni-stuttgart.de")
    httpx_mock.add_response(
        url="https://darus.uni-stuttgart.de/api/datasets/:persistentId/?persistentId=doi:10.18419/X",
        json={"data": {"latestVersion": {"files": []}}},
    )
    async with httpx.AsyncClient() as client:
        files = await dataverse.files(client, "10.18419/X")
    assert files == []


async def test_dataverse_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/TJCLKP",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await dataverse.files(client, "10.7910/DVN/TJCLKP")


@live_only
async def test_live_dataverse_files_have_md5() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await dataverse.files(client, "10.7910/DVN/TJCLKP")
    assert files
    assert all(f.checksum and f.checksum.startswith("md5:") for f in files)
