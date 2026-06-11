"""Safe extraction of downloaded zip/tar archives (opt-in, used by fetch).

Guards every member against path-traversal (zip-slip, tar '../', absolute paths,
symlink/hardlink members) and bounds the cumulative extracted size. A hostile or
runaway archive fails loud rather than writing outside ``dest`` or filling disk.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

from data_aggregator_mcp.errors import FetchTooLargeError, UpstreamUnavailableError

_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz")


def is_archive(name: str) -> bool:
    low = name.lower()
    return low.endswith(".zip") or low.endswith(_TAR_SUFFIXES)


def _safe_dest(dest: Path, member_name: str) -> Path:
    """Resolve a member to a path strictly inside ``dest`` or fail loud."""
    target = (dest / member_name).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise UpstreamUnavailableError(
            f"archive member {member_name!r} escapes the extraction dir — refusing"
        )
    return target


def extract_archive(path: Path, dest: Path, *, max_bytes: int) -> list[Path]:
    """Extract ``path`` (zip or tar*) into ``dest``; return written member paths.

    Fails loud (``UpstreamUnavailableError``) on a traversal/symlink member and
    (``FetchTooLargeError``) when the cumulative extracted size exceeds ``max_bytes``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    total = 0

    _CHUNK = 1 << 16  # 64 KiB

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                out = _safe_dest(dest, info.filename)
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as src, out.open("wb") as fh:
                        while True:
                            chunk = src.read(_CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                fh.close()
                                out.unlink(missing_ok=True)
                                raise FetchTooLargeError(
                                    f"extracted size exceeds max_bytes={max_bytes} for {path.name}"
                                )
                            fh.write(chunk)
                except FetchTooLargeError:
                    raise
                except BaseException:
                    out.unlink(missing_ok=True)
                    raise
                written.append(out)
        return written

    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                if not member.isfile():  # skip dirs; reject symlink/hardlink/device
                    if member.issym() or member.islnk():
                        raise UpstreamUnavailableError(
                            f"archive member {member.name!r} is a link — refusing"
                        )
                    continue
                out = _safe_dest(dest, member.name)
                out.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                try:
                    with extracted, out.open("wb") as fh:
                        while True:
                            chunk = extracted.read(_CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                fh.close()
                                out.unlink(missing_ok=True)
                                raise FetchTooLargeError(
                                    f"extracted size exceeds max_bytes={max_bytes} for {path.name}"
                                )
                            fh.write(chunk)
                except FetchTooLargeError:
                    raise
                except BaseException:
                    out.unlink(missing_ok=True)
                    raise
                written.append(out)
        return written

    raise UpstreamUnavailableError(f"{path.name} is not a recognized zip/tar archive")
