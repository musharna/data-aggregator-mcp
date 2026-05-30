from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import figshare

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_ARTICLE = {
    "id": 31375579,
    "files": [
        {
            "name": "small.csv",
            "size": 123,
            "is_link_only": False,
            "download_url": "https://ndownloader.figshare.com/files/111",
            "computed_md5": "abc123",
        },
        {
            "name": "ext_link",
            "size": 0,
            "is_link_only": True,
            "download_url": "https://ndownloader.figshare.com/files/222",
            "computed_md5": None,
        },
    ],
}


def test_article_id_from_doi() -> None:
    assert figshare._article_id("10.6084/m9.figshare.31375579") == "31375579"
    assert figshare._article_id("10.6084/m9.figshare.31375579.v2") == "31375579"
    assert figshare._article_id("10.5061/dryad.x") is None


async def test_files_parses_article(httpx_mock) -> None:
    httpx_mock.add_response(url="https://api.figshare.com/v2/articles/31375579", json=_ARTICLE)
    async with httpx.AsyncClient() as client:
        files = await figshare.files(client, "10.6084/m9.figshare.31375579.v2")
    assert {f.name for f in files} == {"small.csv"}  # is_link_only dropped
    f = files[0]
    assert f.url == "https://ndownloader.figshare.com/files/111"
    assert f.size == 123
    assert f.checksum == "md5:abc123"


async def test_figshare_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://api.figshare.com/v2/articles/31375579",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await figshare.files(client, "10.6084/m9.figshare.31375579.v2")


@live_only
async def test_live_figshare_manifest_has_md5(monkeypatch) -> None:
    # Figshare's smallest sample file is ~123 MB → manifest-only check, no download.
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await figshare.files(client, "10.6084/m9.figshare.31375579")
    assert files
    assert any(f.checksum and f.checksum.startswith("md5:") for f in files)
