# src/data_aggregator_mcp/sourceio.py
"""The one way ``operate`` reads a record's file: through the egress-guarded client.

operate used to hand the record URL to fsspec (an aiohttp session) and to DuckDB's httpfs.
Both follow redirects by themselves, so the egress guard judged the entry URL and nothing
after it: a public URL answering ``302 Location: http://127.0.0.1/...`` had the private
body read and returned as rows. DuckDB's httpfs cannot be told to stop following redirects
(no such setting among ``duckdb_settings()`` in 1.5), so the fix is not to configure those
clients but to stop handing them remote URLs. Every network read of a source happens
here, through an ``httpx.Client`` whose request hook (``egress.enforce_on_request_sync``)
checks every hop, and the parsers downstream get a file object or a local path:

- ``open_source``: a seekable file; a remote read is one HTTP Range request per read,
  so the Parquet footer and the CSV sniff fetch only what the parser asks for.
- ``local_copy``: a local path for DuckDB; a remote file is downloaded first.
- ``size``: the size the server declares, for operate's ceiling.

``file://`` URLs (operate allows them only behind ``DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS``)
and bare paths are read locally. Every other scheme is refused, so no remote filesystem
fsspec or DuckDB knows (``s3://``, ``hf://``, ...) is reachable from here either.

Synchronous: the callers already run in ``asyncio.to_thread``. The limit in ``egress``
applies unchanged: the name is resolved for the check and again for the connection, so
DNS rebinding between the two is not covered.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import httpx

from data_aggregator_mcp import egress
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError, ValidationError

_TIMEOUT_S = 30.0
# The bytes as stored: under gzip transfer coding the declared length and every Range
# offset would count compressed bytes, not the file's.
_HEADERS = {"Accept-Encoding": "identity"}


def _is_remote(url: str) -> bool:
    scheme = egress.split_url(url, what="operate source").scheme
    if scheme in ("http", "https"):
        return True
    if scheme in ("", "file"):
        return False
    raise ValidationError(
        f"operate source: scheme {scheme!r} cannot be read (only http/https or a local file): {url}"
    )


def _client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=_TIMEOUT_S,
        headers=_HEADERS,
        # Every hop, redirects included: the reason this module exists.
        event_hooks={"request": [egress.enforce_on_request_sync]},
    )


@contextlib.contextmanager
def _transport_errors(url: str) -> Iterator[None]:
    try:
        yield
    except httpx.RequestError as exc:  # transport failures, timeouts, redirect loops
        raise UpstreamUnavailableError(
            f"operate source transport failure: {exc!r} ({url})"
        ) from exc


def _raise_for_status(resp: httpx.Response, url: str) -> None:
    if resp.status_code == 404:
        raise NotFoundError(f"operate source → HTTP 404 ({url})")
    if resp.is_error:
        raise UpstreamUnavailableError(f"operate source → HTTP {resp.status_code} ({url})")


def _declared_length(resp: httpx.Response) -> int | None:
    value = resp.headers.get("Content-Length", "")
    return int(value) if value.isdigit() else None


def _remote_size(client: httpx.Client, url: str) -> int | None:
    with _transport_errors(url):
        resp = client.head(url)
        if resp.is_success:
            return _declared_length(resp)
        # Some servers refuse HEAD (405, or a URL signed for GET only): ask GET for its
        # headers and leave the body unread, as fsspec did.
        with client.stream("GET", url) as resp:
            _raise_for_status(resp, url)
            return _declared_length(resp)


def size(url: str) -> int | None:
    """The source's byte size, or None when the server declares none."""
    if not _is_remote(url):
        import fsspec

        fs, _, paths = fsspec.core.get_fs_token_paths(url)
        local = fs.info(paths[0]).get("size")
        return int(local) if local is not None else None
    with _client() as client:
        return _remote_size(client, url)


class _RangeFile(io.RawIOBase):
    """A read-only, seekable remote file: each read is one Range request through the
    guarded client, with no read-ahead."""

    def __init__(self, client: httpx.Client, url: str) -> None:
        super().__init__()
        self._client = client
        self._url = url
        self._pos = 0
        self._size: int | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def _total(self) -> int:
        if self._size is None:
            self._size = _remote_size(self._client, self._url)
        if self._size is None:
            raise UpstreamUnavailableError(
                f"operate source: the server declares no size for {self._url}, and reading "
                f"this file from its end needs one; use fetch instead"
            )
        return self._size

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._total() + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        if pos < 0:
            raise ValueError(f"negative seek position {pos}")
        self._pos = pos
        return pos

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]  # Buffer is 3.12+
        data = self._get(self._pos, len(buffer)) if len(buffer) else b""
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def _get(self, start: int, want: int) -> bytes:
        headers = {"Range": f"bytes={start}-{start + want - 1}"}
        with (
            _transport_errors(self._url),
            self._client.stream("GET", self._url, headers=headers) as resp,
        ):
            if resp.status_code == 416:
                return b""  # the range starts at or past the end of the file
            _raise_for_status(resp, self._url)
            if resp.status_code != 206 and start > 0:
                raise UpstreamUnavailableError(
                    f"operate source: the server ignores range requests, so {self._url} "
                    f"can only be read whole; use fetch instead"
                )
            # A 200 to a read from the start is the whole file; only its head is taken,
            # so a server that ignores Range cannot make a sniff download everything.
            out = bytearray()
            for chunk in resp.iter_bytes():
                out += chunk
                if len(out) >= want:
                    break
            return bytes(out[:want])


@contextlib.contextmanager
def open_source(url: str) -> Iterator[IO[bytes]]:
    """The source as a seekable binary file (local, or remote through the guard)."""
    if not _is_remote(url):
        import fsspec

        with fsspec.open(url) as local:
            yield local
        return
    with _client() as client, _RangeFile(client, url) as remote:
        yield remote  # type: ignore[misc]  # file-like, not nominally IO


@contextlib.contextmanager
def local_copy(url: str) -> Iterator[str]:
    """A local path holding the source, for a reader that must not see the URL. A local
    source is yielded as given; a remote one is downloaded through the guard into a
    temporary directory removed on exit."""
    if not _is_remote(url):
        yield url
        return
    with tempfile.TemporaryDirectory(prefix="dam-operate-", ignore_cleanup_errors=True) as tmp:
        dest = Path(tmp) / "source"
        with _client() as client, _transport_errors(url), client.stream("GET", url) as resp:
            _raise_for_status(resp, url)
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        yield str(dest)
