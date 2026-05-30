from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import dryad

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_DATASET = {"_links": {"stash:version": {"href": "/api/v2/versions/444628"}}}
_FILES = {
    "_embedded": {
        "stash:files": [
            {
                "path": "tree.tre",
                "size": 4795,
                "digest": "deadbeef",
                "digestType": "sha-256",
                "_links": {"self": {"href": "/api/v2/files/3517groups"}},
            },
        ]
    }
}


async def test_files_two_step_sha256(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.rv15dv4m9",
        json=_DATASET,
    )
    httpx_mock.add_response(url="https://datadryad.org/api/v2/versions/444628/files", json=_FILES)
    async with httpx.AsyncClient() as client:
        files = await dryad.files(client, "10.5061/dryad.rv15dv4m9")
    assert len(files) == 1
    f = files[0]
    assert f.name == "tree.tre"
    assert f.size == 4795
    assert f.checksum == "sha256:deadbeef"  # "sha-256" normalized
    assert f.url == "https://datadryad.org/downloads/file_stream/3517groups"


async def test_dryad_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.rv15dv4m9",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await dryad.files(client, "10.5061/dryad.rv15dv4m9")


@live_only
async def test_live_dryad_manifest_has_sha256() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await dryad.files(client, "10.5061/dryad.rv15dv4m9")
    assert files
    assert any(f.checksum and f.checksum.startswith("sha256:") for f in files)
