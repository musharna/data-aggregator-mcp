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


_VER = "https://datadryad.org/api/v2/versions/444628"


async def test_files_two_step_sha256(httpx_mock) -> None:
    """Positive control: a one-page manifest (no next link) returns exactly its file."""
    httpx_mock.add_response(
        url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.rv15dv4m9",
        json=_DATASET,
    )
    httpx_mock.add_response(url=_VER + "/files?per_page=100", json=_FILES)
    async with httpx.AsyncClient() as client:
        files = await dryad.files(client, "10.5061/dryad.rv15dv4m9")
    assert len(files) == 1
    f = files[0]
    assert f.name == "tree.tre"
    assert f.size == 4795
    assert f.checksum == "sha256:deadbeef"  # "sha-256" normalized
    assert f.url == "https://datadryad.org/downloads/file_stream/3517groups"


def _file(path):
    return {
        "path": path,
        "size": len(path),
        "digest": f"d-{path}",
        "digestType": "sha-256",
        "_links": {"self": {"href": f"/api/v2/files/{path}"}},
    }


def _files_page(paths, next_href=None, total=None):
    links = {"next": {"href": next_href}} if next_href else {}
    return {
        "_links": links,
        "count": len(paths),
        "total": total,
        "_embedded": {"stash:files": [_file(p) for p in paths]},
    }


_DS_URL = "https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.b5mkkwhrk"


async def test_files_follows_next_to_the_last_page(httpx_mock) -> None:
    """A-H5: only page 1 was read, so b5mkkwhrk listed 20 of 31 files and 1c59zw401 20
    of 517. Every next link is followed and the union is the whole manifest."""
    pages = [["a", "b"], ["c", "d"], ["e"]]
    httpx_mock.add_response(url=_DS_URL, json=_DATASET)
    href = "/api/v2/versions/444628/files?page={}&per_page=100"
    httpx_mock.add_response(
        url=_VER + "/files?per_page=100", json=_files_page(pages[0], href.format(2), 5)
    )
    httpx_mock.add_response(
        url="https://datadryad.org" + href.format(2), json=_files_page(pages[1], href.format(3), 5)
    )
    httpx_mock.add_response(
        url="https://datadryad.org" + href.format(3), json=_files_page(pages[2], None, 5)
    )
    async with httpx.AsyncClient() as client:
        files = await dryad.files(client, "10.5061/dryad.b5mkkwhrk")
    names = [f.name for f in files]
    assert names == ["a", "b", "c", "d", "e"]
    assert files[4].url == "https://datadryad.org/downloads/file_stream/e"
    assert files[4].checksum == "sha256:d-e"


async def test_files_short_of_total_raises(httpx_mock) -> None:
    """A walk that ends before the advertised total is a truncated manifest: raise.
    Positive control: the same walk with a matching total succeeds."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    for total in (2, 3):
        httpx_mock.add_response(url=_DS_URL, json=_DATASET)
        httpx_mock.add_response(
            url=_VER + "/files?per_page=100", json=_files_page(["a", "b"], None, total)
        )
    async with httpx.AsyncClient() as client:
        assert [f.name for f in await dryad.files(client, "10.5061/dryad.b5mkkwhrk")] == ["a", "b"]
        with pytest.raises(UpstreamUnavailableError, match=r"Dryad.*b5mkkwhrk.*2 of 3"):
            await dryad.files(client, "10.5061/dryad.b5mkkwhrk")


async def test_files_runaway_pagination_raises(httpx_mock, monkeypatch) -> None:
    """A repeated next link, or more pages than _MAX_PAGES, raises naming Dryad and the
    DOI instead of returning what was read so far. Positive control: a walk that fits
    under the same cap succeeds."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    monkeypatch.setattr(dryad, "_MAX_PAGES", 2)
    first = _VER + "/files?per_page=100"
    p2 = "/api/v2/versions/444628/files?page=2&per_page=100"
    p3 = "/api/v2/versions/444628/files?page=3&per_page=100"
    for _ in range(3):
        httpx_mock.add_response(url=_DS_URL, json=_DATASET)
    # 1: fits (2 pages). 2: third page wanted, over the cap. 3: next points back.
    httpx_mock.add_response(url=first, json=_files_page(["a"], p2, 2))
    httpx_mock.add_response(url="https://datadryad.org" + p2, json=_files_page(["b"], None, 2))
    httpx_mock.add_response(url=first, json=_files_page(["a"], p2, 3))
    httpx_mock.add_response(url="https://datadryad.org" + p2, json=_files_page(["b"], p3, 3))
    httpx_mock.add_response(url=first, json=_files_page(["a"], p2, 3))
    httpx_mock.add_response(
        url="https://datadryad.org" + p2,
        json=_files_page(["b"], first.removeprefix("https://datadryad.org"), 3),
    )
    async with httpx.AsyncClient() as client:
        assert [f.name for f in await dryad.files(client, "10.5061/dryad.b5mkkwhrk")] == ["a", "b"]
        with pytest.raises(UpstreamUnavailableError, match=r"Dryad.*b5mkkwhrk.*2 pages"):
            await dryad.files(client, "10.5061/dryad.b5mkkwhrk")
        monkeypatch.setattr(dryad, "_MAX_PAGES", 10)
        with pytest.raises(UpstreamUnavailableError, match=r"Dryad.*b5mkkwhrk.*revisited"):
            await dryad.files(client, "10.5061/dryad.b5mkkwhrk")


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
async def test_live_dryad_manifest_is_complete() -> None:
    """b5mkkwhrk has 31 files (2 pages at Dryad's default size), 1c59zw401 has 517."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        small = await dryad.files(client, "10.5061/dryad.b5mkkwhrk")
        big = await dryad.files(client, "10.5061/dryad.1c59zw401")
    assert len(small) == len({f.name for f in small}) == 31
    assert len(big) == len({f.url for f in big}) == 517


@live_only
async def test_live_dryad_manifest_has_sha256() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await dryad.files(client, "10.5061/dryad.rv15dv4m9")
    assert files
    assert any(f.checksum and f.checksum.startswith("sha256:") for f in files)
