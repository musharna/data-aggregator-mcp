from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp.errors import FetchTooLargeError
from data_aggregator_mcp.models import DataResource, FileEntry


def _resource(content: bytes) -> DataResource:
    return DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[
            FileEntry(
                name="d.csv",
                size=len(content),
                url="https://zenodo.org/api/records/1/files/d.csv/content",
                checksum=f"md5:{hashlib.md5(content).hexdigest()}",
            )
        ],
    )


async def test_fetch_writes_file_and_sidecar(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    content = b"col1,col2\n1,2\n"
    httpx_mock.add_response(
        url="https://zenodo.org/api/records/1/files/d.csv/content", content=content
    )
    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(client, _resource(content), dest=str(tmp_path))
    assert len(out.paths) == 1
    written = Path(out.paths[0])
    assert written.read_bytes() == content
    assert (written.parent / ".dataresource.json").exists()


async def test_fetch_rejects_over_max_bytes(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    content = b"x" * 100
    async with httpx.AsyncClient() as client:
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client, _resource(content), dest=str(tmp_path), max_bytes=10
            )


async def test_fetch_glob_filters_files(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    content = b"data"
    r = _resource(content)
    r.files.append(FileEntry(name="readme.txt", size=4, url="https://x/readme", checksum=None))
    httpx_mock.add_response(
        url="https://zenodo.org/api/records/1/files/d.csv/content", content=content
    )
    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path), files="*.csv")
    assert len(out.paths) == 1 and out.paths[0].endswith("d.csv")


async def test_concurrent_fetches_to_distinct_dests(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    """Concurrent-write check (async analog of the multiprocessing.Pool default):
    two fetches racing to distinct dirs must both land their file without collision."""
    content = b"data"
    for _ in range(2):
        httpx_mock.add_response(
            url="https://zenodo.org/api/records/1/files/d.csv/content", content=content
        )
    r = _resource(content)
    async with httpx.AsyncClient() as client:
        outs = await asyncio.gather(
            fetch_mod.fetch_files(client, r, dest=str(tmp_path / "a")),
            fetch_mod.fetch_files(client, r, dest=str(tmp_path / "b")),
        )
    assert all(len(o.paths) == 1 for o in outs)
    assert (tmp_path / "a" / "zenodo" / "1" / "d.csv").read_bytes() == content
    assert (tmp_path / "b" / "zenodo" / "1" / "d.csv").read_bytes() == content


async def test_fetch_sanitizes_path_traversal_name(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    """An uploader-controlled file key with parent-dir segments must not escape dest."""
    content = b"evil"
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[
            FileEntry(
                name="../../../escaped.txt",
                size=4,
                url="https://zenodo.org/api/records/1/files/x/content",
                checksum=None,
            )
        ],
    )
    httpx_mock.add_response(url="https://zenodo.org/api/records/1/files/x/content", content=content)
    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))
    for p in out.paths:
        assert Path(p).resolve().is_relative_to(tmp_path.resolve())
    assert not (tmp_path.parent / "escaped.txt").exists()


async def test_fetch_stream_404_raises_not_found(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    from data_aggregator_mcp.errors import NotFoundError

    r = _resource(b"x")
    httpx_mock.add_response(url=r.files[0].url, status_code=404)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await fetch_mod.fetch_files(client, r, dest=str(tmp_path))


async def test_fetch_stream_500_raises_upstream(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    r = _resource(b"x")
    httpx_mock.add_response(url=r.files[0].url, status_code=503)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await fetch_mod.fetch_files(client, r, dest=str(tmp_path))


async def test_fetch_stream_transport_error_raises_upstream(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    r = _resource(b"x")
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=r.files[0].url)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await fetch_mod.fetch_files(client, r, dest=str(tmp_path))


async def test_fetch_extract_unpacks_archive(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"inner"
        info = tarfile.TarInfo("inner.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    content = buf.getvalue()
    r = DataResource(
        id="zenodo:9",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[
            FileEntry(
                name="bundle.tar.gz",
                size=len(content),
                url="https://zenodo.org/api/records/9/files/bundle.tar.gz/content",
                checksum=None,
            )
        ],
    )
    httpx_mock.add_response(url=r.files[0].url, content=content)
    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path), extract=True)
    assert any(p.endswith("inner.txt") for p in out.paths)  # member extracted
    assert any(p.endswith("bundle.tar.gz") for p in out.paths)  # archive kept


def _pdf_resource(url: str) -> DataResource:
    return DataResource(
        id="pubmed:1",
        source="pubmed",
        kind="publication",
        title="t",
        files=[FileEntry(name="fulltext.pdf", mime="application/pdf", url=url, checksum=None)],
    )


async def test_fetch_unverified_html_instead_of_pdf_fails_loud(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    url = "https://pub.example/paywall"
    httpx_mock.add_response(url=url, content=b"<!DOCTYPE html><html><body>Sign in</body></html>")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="not the declared"):
            await fetch_mod.fetch_files(client, _pdf_resource(url), dest=str(tmp_path))
    # the bogus file must not be left on disk
    assert not any(tmp_path.rglob("fulltext.pdf"))


async def test_fetch_unverified_real_pdf_ok(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    url = "https://pub.example/real.pdf"
    httpx_mock.add_response(url=url, content=b"%PDF-1.7\n...bytes...")
    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(client, _pdf_resource(url), dest=str(tmp_path))
    assert out.paths and Path(out.paths[0]).read_bytes().startswith(b"%PDF")
