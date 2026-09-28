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


_ROOT = "https://api.osf.io/v2/nodes/sv3qh/files/osfstorage/"


def _folder(name, href):
    return {
        "attributes": {"kind": "folder", "name": name},
        "relationships": {"files": {"links": {"related": {"href": href}}}},
        "links": {},
    }


async def test_files_walks_every_folder_and_page(httpx_mock) -> None:
    """A-H4: folders were skipped, so osf.io/sv3qh listed 1 of its 12 files. The walk
    must cover every page of every folder, nested ones included, and keep each file's
    relative directory (fetch plans basename collisions on it)."""
    dat = _ROOT + "dat1/"
    code = _ROOT + "code1/"
    raw = _ROOT + "raw1/"
    httpx_mock.add_response(
        url=_ROOT,
        json=_page([_file("Metadata.docx", 1, "f0"), _folder("Datafiles", dat)], _ROOT + "?page=2"),
    )
    httpx_mock.add_response(
        url=_ROOT + "?page=2", json=_page([_folder("R Code", code), _file("top.txt", 2, "f1")])
    )
    httpx_mock.add_response(
        url=dat,
        json=_page([_file("a.csv", 3, "f2"), _folder("raw", raw)], dat + "?page=2"),
    )
    httpx_mock.add_response(url=dat + "?page=2", json=_page([_file("b.csv", 4, "f3")]))
    httpx_mock.add_response(url=raw, json=_page([_file("a.csv", 5, "f4")]))
    httpx_mock.add_response(url=code, json=_page([_file("fit.R", 6, "f5")]))
    async with httpx.AsyncClient() as client:
        files = await osf.files(client, "10.17605/OSF.IO/SV3QH")
    names = [f.name for f in files]
    assert sorted(names) == sorted(
        [
            "Metadata.docx",
            "top.txt",
            "Datafiles/a.csv",
            "Datafiles/b.csv",
            "Datafiles/raw/a.csv",
            "R Code/fit.R",
        ]
    )
    assert len(names) == len(set(names))
    nested = next(f for f in files if f.name == "Datafiles/raw/a.csv")
    assert nested.url == "https://osf.io/download/f4/"
    assert nested.size == 5


async def test_files_flat_single_page_deposit(httpx_mock) -> None:
    """Positive control: a flat one-page deposit returns exactly its files, unprefixed."""
    base = "https://api.osf.io/v2/nodes/5pfej/files/osfstorage/"
    httpx_mock.add_response(
        url=base, json=_page([_file("a.csv", 179, "f1"), _file("b.csv", 200, "f2")])
    )
    async with httpx.AsyncClient() as client:
        files = await osf.files(client, "10.17605/osf.io/5pfej")
    assert [f.name for f in files] == ["a.csv", "b.csv"]
    a = files[0]
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
# Runaway guard: raise, never a silent cap
# ---------------------------------------------------------------------------


async def test_files_request_cap_raises_not_truncates(httpx_mock, monkeypatch) -> None:
    """Past _MAX_REQUESTS the walk raises naming OSF and the node; it used to stop and
    return the pages it had, a silent truncation. Positive control: a walk that fits
    under the same cap still returns every file."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    monkeypatch.setattr(osf, "_MAX_REQUESTS", 2)
    ok = "https://api.osf.io/v2/nodes/okay1/files/osfstorage/"
    httpx_mock.add_response(url=ok, json=_page([_file("a.csv", 1, "f1")], ok + "?page=2"))
    httpx_mock.add_response(url=ok + "?page=2", json=_page([_file("b.csv", 2, "f2")]))
    big = "https://api.osf.io/v2/nodes/abc12/files/osfstorage/"
    httpx_mock.add_response(url=big, json=_page([_file("a.csv", 1, "f1")], big + "?page=2"))
    httpx_mock.add_response(
        url=big + "?page=2", json=_page([_file("b.csv", 2, "f2")], big + "?page=3")
    )
    async with httpx.AsyncClient() as client:
        assert [f.name for f in await osf.files(client, "10.17605/osf.io/okay1")] == [
            "a.csv",
            "b.csv",
        ]
        with pytest.raises(UpstreamUnavailableError, match=r"OSF.*abc12.*2 listing requests"):
            await osf.files(client, "10.17605/osf.io/abc12")


async def test_files_repeated_next_link_raises(httpx_mock) -> None:
    """A next link that points back at a page already read is a loop, not more files."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    base = "https://api.osf.io/v2/nodes/loop1/files/osfstorage/"
    httpx_mock.add_response(url=base, json=_page([_file("a.csv", 1, "f1")], base + "?page=2"))
    httpx_mock.add_response(url=base + "?page=2", json=_page([_file("b.csv", 2, "f2")], base))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match=r"OSF.*loop1.*revisited"):
            await osf.files(client, "10.17605/osf.io/loop1")


@live_only
async def test_live_osf_nested_folders_listed() -> None:
    """osf.io/sv3qh keeps 11 of its 12 files in two folders (Datafiles/, R Code/)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await osf.files(client, "10.17605/OSF.IO/SV3QH")
    names = {f.name for f in files}
    assert len(files) == len(names) == 12
    assert "Metadata.docx" in names
    assert sum(n.startswith("Datafiles/") for n in names) == 10
    assert "R Code/LeafLitter_Final (1).R" in names


@live_only
async def test_live_osf_files_have_md5() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await osf.files(client, "10.17605/osf.io/5pfej")
    assert files
    assert any(f.checksum and f.checksum.startswith("md5:") for f in files)
