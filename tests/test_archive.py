from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from data_aggregator_mcp import archive
from data_aggregator_mcp.errors import FetchTooLargeError, UpstreamUnavailableError


def _make_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_tar_returns_member_paths(tmp_path: Path) -> None:
    arc = tmp_path / "a.tar.gz"
    _make_tar(arc, {"x.txt": b"hello", "y.txt": b"world"})
    dest = tmp_path / "out"
    paths = archive.extract_archive(arc, dest, max_bytes=1000)
    names = sorted(p.name for p in paths)
    assert names == ["x.txt", "y.txt"]
    assert (dest / "x.txt").read_bytes() == b"hello"


def test_extract_zip_returns_member_paths(tmp_path: Path) -> None:
    arc = tmp_path / "a.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("a.csv", "1,2,3")
    dest = tmp_path / "out"
    paths = archive.extract_archive(arc, dest, max_bytes=1000)
    assert [p.name for p in paths] == ["a.csv"]
    assert (dest / "a.csv").read_text() == "1,2,3"


def test_extract_rejects_zip_slip(tmp_path: Path) -> None:
    arc = tmp_path / "evil.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    dest = tmp_path / "out"
    with pytest.raises(UpstreamUnavailableError):
        archive.extract_archive(arc, dest, max_bytes=1000)
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_rejects_tar_traversal(tmp_path: Path) -> None:
    arc = tmp_path / "evil.tar.gz"
    _make_tar(arc, {"../escaped.txt": b"pwned"})
    dest = tmp_path / "out"
    with pytest.raises(UpstreamUnavailableError):
        archive.extract_archive(arc, dest, max_bytes=1000)
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_rejects_oversize(tmp_path: Path) -> None:
    arc = tmp_path / "big.tar.gz"
    _make_tar(arc, {"big.bin": b"x" * 5000})
    dest = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        archive.extract_archive(arc, dest, max_bytes=1000)


def test_is_archive_false_for_plain_file() -> None:
    assert archive.is_archive("data.csv") is False
    assert archive.is_archive("notes.txt") is False


def test_extract_zip_midstream_oversize_unlinks_partial(tmp_path: Path) -> None:
    """When a zip member's ACTUAL written bytes push the running total over max_bytes
    mid-stream, FetchTooLargeError is raised and the partial output file is removed.

    The first member fits (200 bytes < 400 budget); the second member starts writing
    and would push the total to 600 > 400, so extraction must stop mid-member, unlink
    the partial second file, and raise.
    """
    arc = tmp_path / "two.zip"
    with zipfile.ZipFile(arc, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("first.bin", b"A" * 200)  # fits within 400-byte budget
        zf.writestr("second.bin", b"B" * 400)  # pushes cumulative to 600 > 400
    dest = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        archive.extract_archive(arc, dest, max_bytes=400)
    # The partial second file must not exist after the mid-stream abort
    assert not (dest / "second.bin").exists()


def test_extract_tar_midstream_oversize_unlinks_partial(tmp_path: Path) -> None:
    """Same guarantee for tar: when actual written bytes exceed max_bytes mid-stream,
    FetchTooLargeError is raised and the partial output file is removed.

    The first member fits; the second starts writing and would push the total over budget.
    """
    arc = tmp_path / "two.tar.gz"
    _make_tar(arc, {"first.bin": b"A" * 200, "second.bin": b"B" * 400})
    dest = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        archive.extract_archive(arc, dest, max_bytes=400)
    # The partial second file must not exist after the mid-stream abort
    assert not (dest / "second.bin").exists()
