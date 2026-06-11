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


# --- Task 3: resume (idempotent re-fetch) -------------------------------------
#
# These use a hand-rolled httpx.MockTransport rather than the pytest_httpx
# fixture so we can count exactly which URLs were actually requested (to prove a
# resumed file was NOT downloaded again).


def _counting_client(bodies: dict[str, bytes], requested: list[str]) -> httpx.AsyncClient:
    """An AsyncClient whose transport records every requested URL into
    ``requested`` and returns ``bodies[url]`` (404 if absent)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url in bodies:
            return httpx.Response(200, content=bodies[url])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


async def test_fetch_resume_checksum_match_skips_download(tmp_path: Path) -> None:
    content = b"col1,col2\n1,2\n"
    r = _resource(content)
    url = r.files[0].url
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    (target / "d.csv").write_bytes(content)  # pre-existing, checksum-correct

    requested: list[str] = []
    async with _counting_client({url: content}, requested) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))

    assert url not in requested  # NO network request for the resumed file
    assert out.resumed == ["d.csv"]
    assert out.bytes == 0  # nothing transferred
    assert len(out.paths) == 1 and out.paths[0].endswith("d.csv")


async def test_fetch_resume_size_match_skips_download(tmp_path: Path) -> None:
    content = b"abcd"
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name="d.bin", size=4, url="https://x/d.bin", checksum=None)],
    )
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    (target / "d.bin").write_bytes(content)  # size matches, no checksum

    requested: list[str] = []
    async with _counting_client({"https://x/d.bin": content}, requested) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))

    assert "https://x/d.bin" not in requested
    assert out.resumed == ["d.bin"]


async def test_fetch_resume_checksum_mismatch_redownloads(tmp_path: Path) -> None:
    content = b"col1,col2\n1,2\n"
    r = _resource(content)
    url = r.files[0].url
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    (target / "d.csv").write_bytes(b"stale corrupt bytes")  # checksum will NOT match

    requested: list[str] = []
    async with _counting_client({url: content}, requested) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))

    assert url in requested  # re-downloaded
    assert out.resumed == []
    assert (target / "d.csv").read_bytes() == content


async def test_fetch_resume_no_checksum_no_size_redownloads(tmp_path: Path) -> None:
    content = b"unknowable"
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name="d.bin", size=None, url="https://x/d.bin", checksum=None)],
    )
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    (target / "d.bin").write_bytes(content)  # can't verify → must re-download

    requested: list[str] = []
    async with _counting_client({"https://x/d.bin": content}, requested) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))

    assert "https://x/d.bin" in requested
    assert out.resumed == []


async def test_fetch_resume_force_redownloads_despite_match(tmp_path: Path) -> None:
    content = b"col1,col2\n1,2\n"
    r = _resource(content)
    url = r.files[0].url
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    (target / "d.csv").write_bytes(content)  # checksum-correct, but force overrides

    requested: list[str] = []
    async with _counting_client({url: content}, requested) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path), force=True)

    assert url in requested  # force re-downloads
    assert out.resumed == []


# --- Task 4: parallel downloads ----------------------------------------------


class _SlowStream(httpx.AsyncByteStream):
    """Streams ``content`` in chunks, sleeping ``delay`` seconds before each
    chunk so multiple in-flight downloads measurably overlap."""

    def __init__(self, content: bytes, *, chunk: int, delay: float) -> None:
        self._content = content
        self._chunk = chunk
        self._delay = delay

    async def __aiter__(self):
        for i in range(0, len(self._content), self._chunk):
            await asyncio.sleep(self._delay)
            yield self._content[i : i + self._chunk]

    async def aclose(self) -> None:  # noqa: D401
        return None


def _multi_resource(files: list[FileEntry]) -> DataResource:
    return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t", files=files)


async def test_fetch_four_files_all_land_stable_order(tmp_path: Path) -> None:
    bodies = {f"https://x/f{i}.bin": f"body-{i}".encode() for i in range(4)}
    files = [
        FileEntry(
            name=f"f{i}.bin", size=len(b), url=u, checksum=f"md5:{hashlib.md5(b).hexdigest()}"
        )
        for i, (u, b) in enumerate(bodies.items())
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        out = await fetch_mod.fetch_files(client, _multi_resource(files), dest=str(tmp_path))

    target = tmp_path / "zenodo" / "1"
    for i, b in enumerate(bodies.values()):
        assert (target / f"f{i}.bin").read_bytes() == b
    assert len(out.paths) == 4
    assert out.paths == sorted(out.paths)  # stable regardless of completion order


async def test_fetch_parallel_overlaps_in_time(tmp_path: Path) -> None:
    """REAL-EXECUTION concurrency proof: 4 files, each served as several chunks
    with a per-chunk sleep. Bounded-concurrency gather (4 workers) must run them
    overlapping, so wall-time is well under the serial sum. Generous bound to
    avoid flakiness on a loaded CI host."""
    n_files = 4
    chunks_per = 4
    delay = 0.02
    serial = n_files * chunks_per * delay  # ~0.32s if fully sequential
    bodies = {f"https://x/f{i}.bin": (b"z" * 8 * chunks_per) for i in range(n_files)}
    files = [
        FileEntry(name=f"f{i}.bin", size=len(b), url=u, checksum=None)
        for i, (u, b) in enumerate(bodies.items())
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        body = bodies[str(request.url)]
        return httpx.Response(200, stream=_SlowStream(body, chunk=8, delay=delay))

    import time

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        t0 = time.monotonic()
        out = await fetch_mod.fetch_files(client, _multi_resource(files), dest=str(tmp_path))
        elapsed = time.monotonic() - t0

    assert len(out.paths) == n_files
    # 4 workers ⇒ all 4 files overlap; expected wall ≈ one file's time (~0.08s).
    assert elapsed < 0.6 * serial, f"no overlap: {elapsed:.3f}s vs serial {serial:.3f}s"


async def test_fetch_parallel_one_404_raises_and_cleans_partial(tmp_path: Path) -> None:
    """One file 404s while siblings are mid-stream. The whole fetch must raise
    (fail-loud), the 404'd file leaves no partial, and the run does not hang."""
    from data_aggregator_mcp.errors import NotFoundError

    # Siblings stream slowly (long per-chunk sleep) so the immediate 404
    # cancels them while they are still mid-stream — exercising the
    # CancelledError → partial-cleanup path rather than a clean completion.
    delay = 0.25
    good = {f"https://x/f{i}.bin": (b"z" * 320) for i in range(3)}
    bad_url = "https://x/bad.bin"
    files = [
        FileEntry(name=f"f{i}.bin", size=320, url=u, checksum=None) for i, u in enumerate(good)
    ]
    files.append(FileEntry(name="bad.bin", size=320, url=bad_url, checksum=None))

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == bad_url:
            return httpx.Response(404)
        return httpx.Response(200, stream=_SlowStream(good[url], chunk=8, delay=delay))

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(NotFoundError):
            await asyncio.wait_for(
                fetch_mod.fetch_files(client, _multi_resource(files), dest=str(tmp_path)),
                timeout=5.0,  # guards against a hang
            )

    target = tmp_path / "zenodo" / "1"
    assert not (target / "bad.bin").exists()  # 404 partial removed
    # Cancellation cleanup is load-bearing: the slow siblings were cancelled
    # mid-stream (their first chunk is gated behind asyncio.sleep), so their
    # BaseException(CancelledError) handler must have unlinked each partial. No
    # *.bin file may survive a failed fetch.
    assert sorted(p.name for p in target.glob("*.bin")) == []


async def test_fetch_parallel_under_declared_stream_blows_budget(tmp_path: Path) -> None:
    """A file under-declares its size; the real stream blows the shared byte
    budget → FetchTooLargeError (fail-loud), even under concurrency."""
    big = b"y" * 10_000
    # Declare a tiny size so the upfront pre-check passes, then stream far more.
    files = [
        FileEntry(name="a.bin", size=10, url="https://x/a.bin", checksum=None),
        FileEntry(name="b.bin", size=10, url="https://x/b.bin", checksum=None),
    ]
    bodies = {"https://x/a.bin": big, "https://x/b.bin": big}

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[str(request.url)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client, _multi_resource(files), dest=str(tmp_path), max_bytes=100
            )


# --- Task 5: progress notifications ------------------------------------------


async def test_fetch_invokes_on_progress_once_per_file(tmp_path: Path) -> None:
    """on_progress is awaited once per file with a running done-count covering
    1..N and a constant total==N. Completion order under concurrency is
    nondeterministic, so assert the SET of done-values rather than strict order."""
    n = 5
    bodies = {f"https://x/f{i}.bin": f"body-{i}".encode() for i in range(n)}
    files = [
        FileEntry(
            name=f"f{i}.bin", size=len(b), url=u, checksum=f"md5:{hashlib.md5(b).hexdigest()}"
        )
        for i, (u, b) in enumerate(bodies.items())
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[str(request.url)])

    calls: list[tuple[int, int, str]] = []

    async def _recorder(done: int, total: int, name: str) -> None:
        calls.append((done, total, name))

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        out = await fetch_mod.fetch_files(
            client, _multi_resource(files), dest=str(tmp_path), on_progress=_recorder
        )

    assert len(out.paths) == n
    assert len(calls) == n  # once per file
    dones = sorted(c[0] for c in calls)
    assert dones == list(range(1, n + 1))  # done covers 1..N
    assert {c[1] for c in calls} == {n}  # total constant == N
    assert {c[2] for c in calls} == {f"f{i}.bin" for i in range(n)}


async def test_fetch_none_on_progress_is_noop(tmp_path: Path) -> None:
    """on_progress=None (the default) must not break the fetch."""
    body = b"hello"
    files = [
        FileEntry(
            name="a.bin",
            size=len(body),
            url="https://x/a.bin",
            checksum=f"md5:{hashlib.md5(body).hexdigest()}",
        )
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        out = await fetch_mod.fetch_files(
            client, _multi_resource(files), dest=str(tmp_path), on_progress=None
        )
    assert len(out.paths) == 1


# --- Fix 2: extraction budget shares download budget -------------------------


async def test_fetch_extract_budget_shared_with_download(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """Extraction max_bytes must be the REMAINING budget after the download, not
    the original cap. If the download already consumed most of the budget, an
    archive whose extracted content would exceed the remainder must raise
    FetchTooLargeError — not silently write up to 2x max_bytes total."""
    import io
    import tarfile as tarfile_mod

    # plain_body is 400 bytes (download budget cost: 400).
    # arc_inner is 500 bytes uncompressed but compresses to ~105 bytes (gzip).
    # Total download cost: ~505 bytes → well under max_bytes=1000.
    # Remaining budget after downloads: ~495 bytes.
    # Extraction needs 500 bytes → must fail with shared budget, but would succeed
    # if extraction receives the full original cap (1000) instead of the remainder.
    # size=None on both entries so the pre-flight declared-total check cannot interfere.
    plain_body = b"P" * 400  # 400-byte plain file; download cost ≈ 400
    arc_inner = b"I" * 500  # 500 bytes uncompressed; ~105 bytes compressed

    buf = io.BytesIO()
    with tarfile_mod.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile_mod.TarInfo("inner.bin")
        info.size = len(arc_inner)
        tf.addfile(info, io.BytesIO(arc_inner))
    arc_body = buf.getvalue()
    # Verify the test invariant: downloads fit, extraction alone busts the remainder.
    assert len(plain_body) + len(arc_body) < 1000, "downloads must fit within budget"
    assert len(arc_inner) > 1000 - len(plain_body) - len(arc_body), (
        "extraction must exceed remainder"
    )

    # size=None on all entries so the pre-flight declared-total check is bypassed;
    # only the shared runtime budget can enforce the ceiling.
    resource = DataResource(
        id="zenodo:99",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[
            FileEntry(name="plain.bin", size=None, url="https://x/plain.bin"),
            FileEntry(name="bundle.tar.gz", size=None, url="https://x/bundle.tar.gz"),
        ],
    )
    httpx_mock.add_response(url="https://x/plain.bin", content=plain_body)
    httpx_mock.add_response(url="https://x/bundle.tar.gz", content=arc_body)

    async with httpx.AsyncClient() as client:
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client, resource, dest=str(tmp_path), max_bytes=1000, extract=True
            )


async def test_fetch_extract_small_archive_within_budget_succeeds(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """A small archive whose extracted content fits within the remaining budget
    must succeed normally — the shared-budget fix must not break the happy path."""
    import io
    import tarfile as tarfile_mod

    arc_inner = b"OK" * 10  # 20 bytes extracted

    buf = io.BytesIO()
    with tarfile_mod.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile_mod.TarInfo("inner.txt")
        info.size = len(arc_inner)
        tf.addfile(info, io.BytesIO(arc_inner))
    arc_body = buf.getvalue()

    resource = DataResource(
        id="zenodo:88",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name="tiny.tar.gz", size=len(arc_body), url="https://x/tiny.tar.gz")],
    )
    httpx_mock.add_response(url="https://x/tiny.tar.gz", content=arc_body)

    async with httpx.AsyncClient() as client:
        out = await fetch_mod.fetch_files(
            client, resource, dest=str(tmp_path), max_bytes=10_000, extract=True
        )
    assert any(p.endswith("inner.txt") for p in out.paths)


# --- Fix 3: URL scheme allowlist for downloads --------------------------------


async def test_fetch_rejects_file_scheme_url(tmp_path: Path) -> None:
    """A FileEntry with a file:// URL must be rejected with UpstreamUnavailableError
    before any I/O — a poisoned metadata record must not read local files."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    resource = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name="passwd", url="file:///etc/passwd")],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="file://"):
            await fetch_mod.fetch_files(client, resource, dest=str(tmp_path))
    # Nothing should have been written
    assert not list(tmp_path.rglob("passwd"))
