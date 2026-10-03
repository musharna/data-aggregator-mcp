"""Safe extraction of downloaded zip/tar archives (opt-in, used by fetch).

The archive's name decides its format: ``is_archive`` and ``extract_archive`` read the
same suffix tables. Sniffing the bytes cannot decide it, because ``zipfile.is_zipfile``
finds a zip's end record anywhere in the last 64 KiB, so a plain ``.tar`` whose last
member is a ``.zip`` sniffs as that zip.

Guards every member against path-traversal (zip-slip, tar '../', absolute paths,
symlink/hardlink members) and bounds the cumulative extracted size. A hostile, runaway
or unreadable archive fails loud rather than writing outside ``dest`` or filling disk,
and a failed extraction removes every member it wrote.
"""

from __future__ import annotations

import tarfile
import zipfile
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import IO

from data_aggregator_mcp.errors import FetchTooLargeError, UpstreamUnavailableError

_ZIP_SUFFIXES = (".zip",)
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz")
_ARCHIVE_SUFFIXES = _ZIP_SUFFIXES + _TAR_SUFFIXES
_CHUNK = 1 << 16  # 64 KiB: the most of one member held in memory at a time

_Members = Generator[tuple[str, IO[bytes]], None, None]


def is_archive(name: str) -> bool:
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


@contextmanager
def _reading(path: Path) -> Iterator[None]:
    """Report a failure to decode the archive's bytes (corrupt, truncated, encrypted, an
    unsupported compression method) as the archive's, not as a bare library error."""
    try:
        yield
    except Exception as exc:
        raise UpstreamUnavailableError(
            f"{path.name} could not be read as an archive ({type(exc).__name__}: {exc})"
        ) from exc


def _safe_dest(root: Path, member_name: str) -> Path:
    """Resolve a member to a file path strictly inside ``root`` or fail loud."""
    target = (root / member_name).resolve()
    if target == root or not target.is_relative_to(root):
        raise UpstreamUnavailableError(
            f"archive member {member_name!r} escapes the extraction dir — refusing"
        )
    return target


def _zip_members(path: Path) -> _Members:
    with _reading(path):
        zf = zipfile.ZipFile(path)
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with _reading(path):
                src = zf.open(info)
            yield info.filename, src


def _tar_members(path: Path) -> _Members:
    with _reading(path):
        # Opened apart from its `with` so that only a failure to open is the archive's.
        tf = tarfile.open(path)  # noqa: SIM115 - closed by the `with tf` below
    with tf:
        with _reading(path):
            members = tf.getmembers()
        for member in members:
            if member.issym() or member.islnk():
                raise UpstreamUnavailableError(
                    f"archive member {member.name!r} is a link — refusing"
                )
            if not member.isfile():  # a directory, device or fifo: nothing to write
                continue
            src = tf.extractfile(member)  # builds a reader; a failure surfaces on read
            assert src is not None  # extractfile returns None only for a non-file member
            yield member.name, src


def _chunks(src: IO[bytes], path: Path) -> Iterator[bytes]:
    while True:
        with _reading(path):
            chunk = src.read(_CHUNK)
        if not chunk:
            return
        yield chunk


def extract_archive(path: Path, dest: Path, *, max_bytes: int) -> list[Path]:
    """Extract ``path`` (zip or tar*, by its name) into ``dest``; return the written paths.

    Fails loud (``UpstreamUnavailableError``) on a traversal/link member or an unreadable
    archive, and (``FetchTooLargeError``) when the cumulative extracted size exceeds
    ``max_bytes``. On any failure, every member written so far is removed.
    """
    low = path.name.lower()
    if low.endswith(_ZIP_SUFFIXES):
        members = _zip_members(path)
    elif low.endswith(_TAR_SUFFIXES):
        members = _tar_members(path)
    else:
        raise UpstreamUnavailableError(f"{path.name} is not named as a zip/tar archive")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written: dict[Path, None] = {}  # insertion-ordered set: a repeated member is one file
    total = 0
    try:
        with closing(members):
            for name, src in members:
                out = _safe_dest(root, name)
                out.parent.mkdir(parents=True, exist_ok=True)
                with src, out.open("wb") as fh:
                    written.setdefault(out)
                    for chunk in _chunks(src, path):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchTooLargeError(
                                f"extracted size exceeds max_bytes={max_bytes} for {path.name}"
                            )
                        fh.write(chunk)
    except BaseException:
        for p in written:
            p.unlink()
        raise
    return list(written)
