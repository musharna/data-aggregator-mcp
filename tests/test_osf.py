from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import osf

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _page(items, next_url=None):
    return {"data": items, "links": {"next": next_url}}


def _file(name, size, fid, md5="m"):
    return {
        "attributes": {
            "kind": "file",
            "name": name,
            "size": size,
            "extra": {"hashes": {"md5": md5}},
        },
        "links": {"download": f"https://osf.io/download/{fid}/"},
    }


_FOLDER = {"attributes": {"kind": "folder", "name": "sub"}, "links": {}}


async def test_files_paginates_and_filters_folders(httpx_mock) -> None:
    base = "https://api.osf.io/v2/nodes/5pfej/files/osfstorage/"
    page2 = base + "?page=2"
    httpx_mock.add_response(url=base, json=_page([_file("a.csv", 179, "f1"), _FOLDER], page2))
    httpx_mock.add_response(url=page2, json=_page([_file("b.csv", 200, "f2")]))
    async with httpx.AsyncClient() as client:
        files = await osf.files(client, "10.17605/osf.io/5pfej")
    assert {f.name for f in files} == {"a.csv", "b.csv"}  # folder dropped, page 2 included
    a = next(f for f in files if f.name == "a.csv")
    assert a.url == "https://osf.io/download/f1/"
    assert a.size == 179
    assert a.checksum == "md5:m"


def test_guid_from_doi() -> None:
    assert osf._guid("10.17605/osf.io/5pfej") == "5pfej"
    assert osf._guid("10.17605/OSF.IO/5PFEJ") == "5pfej"


async def test_osf_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://api.osf.io/v2/nodes/5pfej/files/osfstorage/",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await osf.files(client, "10.17605/osf.io/5pfej")


# ---------------------------------------------------------------------------
# Fix — file-listing loop must cap at _MAX_FILE_PAGES
# ---------------------------------------------------------------------------


async def test_files_page_cap_limits_requests(httpx_mock, monkeypatch) -> None:
    """With _MAX_FILE_PAGES monkeypatched to 2, only 2 pages should be fetched
    even when the second page advertises a third via links.next."""
    monkeypatch.setattr(osf, "_MAX_FILE_PAGES", 2)

    base = "https://api.osf.io/v2/nodes/abc12/files/osfstorage/"
    page2 = base + "?page=2"
    page3 = base + "?page=3"

    httpx_mock.add_response(url=base, json=_page([_file("a.csv", 100, "f1")], page2))
    httpx_mock.add_response(url=page2, json=_page([_file("b.csv", 200, "f2")], page3))
    # page3 should never be fetched — if it is, httpx_mock will raise

    async with httpx.AsyncClient() as client:
        files = await osf.files(client, "10.17605/osf.io/abc12")

    assert {f.name for f in files} == {"a.csv", "b.csv"}


@live_only
async def test_live_osf_files_have_md5() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await osf.files(client, "10.17605/osf.io/5pfej")
    assert files
    assert any(f.checksum and f.checksum.startswith("md5:") for f in files)
