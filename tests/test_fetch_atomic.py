"""A fetch never damages a file already on disk.

``fetch`` used to stream straight into the target path and unlink that path on any
failure. A re-fetch of a file it cannot verify as complete (no checksum, no size: every
GEO supplementary file) therefore truncated a good copy on open and deleted it when the
download then failed. Seen live in the 2026-10-04 head-to-head (task T3): a second
``fetch`` with ``max_bytes=1000`` raised ``FetchTooLargeError`` and the good file was gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import server
from data_aggregator_mcp.errors import (
    FetchTooLargeError,
    NotFoundError,
    UpstreamUnavailableError,
)
from data_aggregator_mcp.models import DataResource, FileEntry

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"

OLD = b"the good copy already on disk\n" * 10
NEW = b"a newer upstream copy\n" * 10


def _record(*files: FileEntry) -> DataResource:
    return DataResource(
        id="zenodo:1", source="zenodo", kind="dataset", title="t", files=list(files)
    )


def _serving(bodies: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return bodies.get(str(request.url)) or httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.startswith(".fetch-"))


def _md5(b: bytes) -> str:
    return "md5:" + hashlib.md5(b, usedforsecurity=False).hexdigest()


# (failure, entry, the second fetch's server response, fetch kwargs, expected error)
FAILURES = [
    (
        "over max_bytes mid-stream",
        FileEntry(name="counts.txt.gz", url="https://h/counts.txt.gz"),
        httpx.Response(200, content=NEW),
        {"max_bytes": 100},
        FetchTooLargeError,
    ),
    (
        "HTTP 503",
        FileEntry(name="counts.txt.gz", url="https://h/counts.txt.gz"),
        httpx.Response(503),
        {},
        UpstreamUnavailableError,
    ),
    (
        "HTML served for a PDF",
        FileEntry(name="paper.pdf", url="https://h/paper.pdf", mime="application/pdf"),
        httpx.Response(200, content=b"<!doctype html><p>sign in"),
        {},
        UpstreamUnavailableError,
    ),
    (
        "checksum mismatch on a forced re-fetch",
        FileEntry(name="data.bin", url="https://h/data.bin", checksum=_md5(OLD)),
        httpx.Response(200, content=NEW),
        {"force": True},
        UpstreamUnavailableError,
    ),
]


@pytest.mark.parametrize(
    ("entry", "response", "kwargs", "error"),
    [f[1:] for f in FAILURES],
    ids=[f[0] for f in FAILURES],
)
async def test_a_failed_refetch_leaves_the_good_copy_untouched(
    tmp_path: Path, entry: FileEntry, response: httpx.Response, kwargs: dict, error: type
) -> None:
    target = tmp_path / "zenodo" / "1"
    out = target / entry.name
    async with _serving({entry.url: httpx.Response(200, content=OLD)}) as client:
        first = await fetch_mod.fetch_files(client, _record(entry), dest=str(tmp_path))
    assert first.paths == [str(out)] and out.read_bytes() == OLD

    async with _serving({entry.url: response}) as client:
        with pytest.raises(error):
            await fetch_mod.fetch_files(client, _record(entry), dest=str(tmp_path), **kwargs)
    assert out.read_bytes() == OLD  # the failure did not touch the good copy
    assert _leftovers(target) == []  # and left no temp file behind

    # Positive control: a re-fetch that succeeds still writes the file. With a declared
    # checksum only the matching bytes can succeed, so that case re-serves OLD.
    body = OLD if entry.checksum else NEW
    async with _serving({entry.url: httpx.Response(200, content=body)}) as client:
        again = await fetch_mod.fetch_files(client, _record(entry), dest=str(tmp_path), force=True)
    assert again.paths == [str(out)]
    assert out.read_bytes() == body
    assert _leftovers(target) == []


async def test_a_refetch_cancelled_by_a_failing_sibling_keeps_the_good_copy(
    tmp_path: Path,
) -> None:
    """The cancellation path: a sibling's 404 cancels this file's re-download mid-stream."""
    started = asyncio.Event()

    async def slow():
        yield NEW[:8]
        started.set()
        await asyncio.sleep(5)  # cancelled long before this ends
        yield NEW[8:]

    keep = FileEntry(name="keep.txt", url="https://h/keep.txt")
    gone = FileEntry(name="gone.txt", url="https://h/gone.txt")
    target = tmp_path / "zenodo" / "1"
    async with _serving({keep.url: httpx.Response(200, content=OLD)}) as client:
        await fetch_mod.fetch_files(client, _record(keep), dest=str(tmp_path))
    assert (target / "keep.txt").read_bytes() == OLD

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == keep.url:
            return httpx.Response(200, content=slow())
        return httpx.Response(404)

    async def fail_once_started(request: httpx.Request) -> httpx.Response:
        if str(request.url) == gone.url:
            await started.wait()  # the 404 lands while keep.txt is mid-stream
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_once_started)) as client:
        with pytest.raises(NotFoundError):
            await asyncio.wait_for(
                fetch_mod.fetch_files(client, _record(keep, gone), dest=str(tmp_path)), 5
            )
    assert started.is_set()  # the re-download really was under way when it was cancelled
    assert (target / "keep.txt").read_bytes() == OLD
    assert not (target / "gone.txt").exists()
    assert _leftovers(target) == []


async def test_the_target_holds_the_old_copy_until_the_new_one_is_complete(
    tmp_path: Path,
) -> None:
    """A reader of the target path never sees a truncated or half-written file."""
    entry = FileEntry(name="counts.txt", url="https://h/counts.txt")
    out = tmp_path / "zenodo" / "1" / "counts.txt"
    async with _serving({entry.url: httpx.Response(200, content=OLD)}) as client:
        await fetch_mod.fetch_files(client, _record(entry), dest=str(tmp_path))

    seen_mid_stream: list[bytes] = []

    async def chunks():
        yield NEW[:10]
        await asyncio.sleep(0)
        seen_mid_stream.append(out.read_bytes())
        yield NEW[10:]

    async with _serving({entry.url: httpx.Response(200, content=chunks())}) as client:
        await fetch_mod.fetch_files(client, _record(entry), dest=str(tmp_path))
    assert seen_mid_stream == [OLD]
    assert out.read_bytes() == NEW  # positive control: the new copy did land


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_a_failed_geo_refetch_keeps_the_downloaded_file(tmp_path: Path) -> None:
    """The head-to-head's T3 miss, replayed against GEO. Its supplementary files carry no
    checksum or size, so the second fetch really downloads again and fails mid-stream."""
    name = "GSE241827_counts_dds_v5.txt.gz"
    sha = "8837dc6e5616561f1ed75ae558a04f3b9244e7b2bce4b1535a030644aa131800"
    args = {"id": "geo:GSE241827", "dest": str(tmp_path), "files": name}
    first = await server._dispatch("fetch", {**args, "max_bytes": 10_000_000})
    (path,) = first["paths"]
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
    with pytest.raises(FetchTooLargeError, match="stream exceeded max_bytes"):
        await server._dispatch("fetch", {**args, "max_bytes": 1000})
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
    assert _leftovers(Path(path).parent) == []
