"""Observed behaviour of ``archive``: the format each name selects, every refusal with its
legitimate counterpart beside it, what a failed extraction leaves behind, and real
archives fetched from a live Zenodo record."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import tarfile
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import archive, router
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp.errors import FetchTooLargeError, UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _zip(path: Path, members: dict[str, bytes], *, dirs: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for d in dirs:
            zf.writestr(zipfile.ZipInfo(d), b"")
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _tar(path: Path, entries: list[tarfile.TarInfo | tuple[str, bytes]], mode: str = "w") -> Path:
    with tarfile.open(path, mode) as tf:
        for e in entries:
            if isinstance(e, tarfile.TarInfo):
                tf.addfile(e)
            else:
                info = tarfile.TarInfo(e[0])
                info.size = len(e[1])
                tf.addfile(info, io.BytesIO(e[1]))
    return path


def _typed(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _rel(paths: list[Path], root: Path) -> list[str]:
    return sorted(p.relative_to(root.resolve()).as_posix() for p in paths)


def _msg(text: str) -> str:
    return "^" + re.escape(text) + "$"


def _unreadable(name: str, cause: type[BaseException]) -> str:
    """Our message exactly, then the library's own type and text, which vary by Python."""
    return (
        "(?s)^"
        + re.escape(f"[UpstreamUnavailableError] {name} could not be read as an archive ")
        + re.escape(f"({cause.__name__}: ")
        + ".+\\)$"
    )


# --- which format a name selects -------------------------------------------------


def test_a_tar_whose_last_member_is_a_zip_extracts_as_the_tar(tmp_path: Path) -> None:
    """``zipfile.is_zipfile`` finds a zip's end record anywhere in the last 64 KiB, so a
    plain .tar ending in a .zip member used to extract as that inner zip: the tar's own
    members were dropped and the inner zip's written in their place."""
    inner = _zip(tmp_path / "inner.zip", {"inner.txt": b"inner"}).read_bytes()
    tar = _tar(tmp_path / "bundle.tar", [("real.txt", b"real"), ("bundle.zip", inner)])
    assert zipfile.is_zipfile(tar)  # the sniff that misled the old dispatch

    dest = tmp_path / "out"
    got = archive.extract_archive(tar, dest, max_bytes=10_000)
    assert _rel(got, dest) == ["bundle.zip", "real.txt"]
    assert (dest / "real.txt").read_bytes() == b"real"
    assert (dest / "bundle.zip").read_bytes() == inner
    assert not (dest / "inner.txt").exists()


def test_the_name_decides_the_format_in_any_case(tmp_path: Path) -> None:
    z = _zip(tmp_path / "UPPER.ZIP", {"z.txt": b"z"})
    t = _tar(tmp_path / "UPPER.TGZ", [("t.txt", b"t")], mode="w:gz")
    assert _rel(archive.extract_archive(z, tmp_path / "z", max_bytes=100), tmp_path / "z") == [
        "z.txt"
    ]
    assert _rel(archive.extract_archive(t, tmp_path / "t", max_bytes=100), tmp_path / "t") == [
        "t.txt"
    ]
    # Each suffix family is read by its own reader: tar bytes named .zip and zip bytes
    # named .tar.gz are refused, not re-sniffed into the other format.
    tar_named_zip = tmp_path / "tar.zip"
    shutil.copy(t, tar_named_zip)
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("tar.zip", zipfile.BadZipFile),
    ):
        archive.extract_archive(tar_named_zip, tmp_path / "a", max_bytes=100)
    zip_named_tgz = tmp_path / "zip.tar.gz"
    shutil.copy(z, zip_named_tgz)
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("zip.tar.gz", tarfile.ReadError),
    ):
        archive.extract_archive(zip_named_tgz, tmp_path / "b", max_bytes=100)
    csv = tmp_path / "data.csv"
    shutil.copy(z, csv)
    with pytest.raises(
        UpstreamUnavailableError,
        match=_msg("[UpstreamUnavailableError] data.csv is not named as a zip/tar archive"),
    ):
        archive.extract_archive(csv, tmp_path / "c", max_bytes=100)
    assert not (tmp_path / "c").exists()  # refused before anything was created


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.zip", True),
        ("A.ZIP", True),
        ("a.tar", True),
        ("a.tar.gz", True),
        ("a.TGZ", True),
        ("a.tar.bz2", True),
        ("a.tbz2", True),
        ("a.tar.xz", True),
        ("a.gz", False),
        ("a.zip.txt", False),
        ("zip", False),
    ],
)
def test_is_archive_reads_the_suffix_tables(name: str, expected: bool) -> None:
    assert archive.is_archive(name) is expected


# --- unreadable archives -----------------------------------------------------------


def _corrupt_second_member(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    at = raw.index(b"B" * 100)
    raw[at + 5] = ord("C")
    path.write_bytes(bytes(raw))


def test_a_corrupt_member_is_upstream_trouble_and_leaves_nothing(tmp_path: Path) -> None:
    good = _zip(tmp_path / "good.zip", {"a.txt": b"A" * 100, "b.txt": b"B" * 100})
    bad = tmp_path / "bad.zip"
    shutil.copy(good, bad)
    _corrupt_second_member(bad)

    ok = archive.extract_archive(good, tmp_path / "ok", max_bytes=1000)
    assert _rel(ok, tmp_path / "ok") == ["a.txt", "b.txt"]
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("bad.zip", zipfile.BadZipFile),
    ) as caught:
        archive.extract_archive(bad, tmp_path / "out", max_bytes=1000)
    assert isinstance(caught.value.__cause__, zipfile.BadZipFile)
    assert _files(tmp_path / "out") == []  # a.txt, written before b.txt failed, is gone


def test_a_truncated_tar_gz_is_upstream_trouble(tmp_path: Path) -> None:
    data = bytes(range(256)) * 400
    good = _tar(tmp_path / "good.tar.gz", [("a.bin", data), ("b.bin", data)], mode="w:gz")
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size * 2 // 3])

    ok = archive.extract_archive(good, tmp_path / "ok", max_bytes=10**6)
    assert _rel(ok, tmp_path / "ok") == ["a.bin", "b.bin"]
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("cut.tar.gz", EOFError),
    ):
        archive.extract_archive(cut, tmp_path / "out", max_bytes=10**6)
    assert _files(tmp_path / "out") == []


def _set_zip_header_fields(path: Path, *, flags: int | None = None, method: int | None = None):
    """Rewrite the general-purpose flags / compression method of every entry, in both the
    local headers and the central directory (stored members, so offsets are stable)."""
    raw = bytearray(path.read_bytes())
    for sig, flag_at, method_at in ((b"PK\x03\x04", 6, 8), (b"PK\x01\x02", 8, 10)):
        start = 0
        while (at := raw.find(sig, start)) >= 0:
            if flags is not None:
                raw[at + flag_at : at + flag_at + 2] = flags.to_bytes(2, "little")
            if method is not None:
                raw[at + method_at : at + method_at + 2] = method.to_bytes(2, "little")
            start = at + 4
    path.write_bytes(bytes(raw))


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        # Deflate64 (method 9): what Windows writes for large "compressed folders".
        ({"method": 9}, NotImplementedError),
        ({"flags": 1}, RuntimeError),  # encrypted
    ],
)
def test_a_zip_python_cannot_decode_is_upstream_trouble(
    tmp_path: Path, change: dict[str, int], cause: type[BaseException]
) -> None:
    good = _zip(tmp_path / "good.zip", {"a.txt": b"A" * 100})
    odd = tmp_path / "odd.zip"
    shutil.copy(good, odd)
    _set_zip_header_fields(odd, **change)

    assert _rel(archive.extract_archive(good, tmp_path / "ok", max_bytes=1000), tmp_path / "ok")
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("odd.zip", cause),
    ) as caught:
        archive.extract_archive(odd, tmp_path / "out", max_bytes=1000)
    assert type(caught.value.__cause__) is cause
    assert _files(tmp_path / "out") == []


# --- members that must not be written ----------------------------------------------


@pytest.mark.parametrize("bad", ["../escaped.txt", "a/../../escaped.txt", "a/..", "."])
def test_a_zip_member_outside_the_dir_is_refused_beside_one_inside(
    tmp_path: Path, bad: str
) -> None:
    dest = tmp_path / "out"
    fine = _zip(tmp_path / "fine.zip", {"a/../inside.txt": b"ok"})
    assert _rel(archive.extract_archive(fine, dest, max_bytes=100), dest) == ["inside.txt"]

    evil = _zip(tmp_path / "evil.zip", {"first.txt": b"1", bad: b"pwned"})
    with pytest.raises(
        UpstreamUnavailableError,
        match=_msg(
            f"[UpstreamUnavailableError] archive member {bad!r} escapes the extraction dir"
            " — refusing"
        ),
    ):
        archive.extract_archive(evil, tmp_path / "evil", max_bytes=100)
    assert not (tmp_path / "escaped.txt").exists()
    assert _files(tmp_path / "evil") == []


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_an_absolute_member_path_is_refused(tmp_path: Path, kind: str) -> None:
    victim = tmp_path / "victim.txt"
    if kind == "zip":
        good = _zip(tmp_path / "good.zip", {"victim.txt": b"fine"})
        evil = _zip(tmp_path / "evil.zip", {str(victim): b"pwned"})
    else:
        good = _tar(tmp_path / "good.tar", [("victim.txt", b"fine")])
        evil = _tar(tmp_path / "evil.tar", [(str(victim), b"pwned")])
    assert archive.extract_archive(good, tmp_path / "ok", max_bytes=100) == [
        (tmp_path / "ok" / "victim.txt").resolve()
    ]
    with pytest.raises(
        UpstreamUnavailableError,
        match=_msg(
            f"[UpstreamUnavailableError] archive member {str(victim)!r} escapes the "
            "extraction dir — refusing"
        ),
    ):
        archive.extract_archive(evil, tmp_path / "out", max_bytes=100)
    assert not victim.exists()


@pytest.mark.parametrize(
    ("kind", "linkname"),
    [(tarfile.SYMTYPE, "/etc/passwd"), (tarfile.LNKTYPE, "plain.txt")],
)
def test_a_tar_link_member_is_refused_beside_a_plain_tar(
    tmp_path: Path, kind: bytes, linkname: str
) -> None:
    plain = _tar(tmp_path / "plain.tar", [("plain.txt", b"p")])
    assert _rel(archive.extract_archive(plain, tmp_path / "ok", max_bytes=100), tmp_path / "ok")

    linked = _tar(
        tmp_path / "linked.tar", [("plain.txt", b"p"), _typed("link.txt", kind, linkname)]
    )
    with pytest.raises(
        UpstreamUnavailableError,
        match=_msg("[UpstreamUnavailableError] archive member 'link.txt' is a link — refusing"),
    ):
        archive.extract_archive(linked, tmp_path / "out", max_bytes=100)
    assert _files(tmp_path / "out") == []
    assert not (tmp_path / "out" / "link.txt").is_symlink()


# --- what is written, and where ----------------------------------------------------


def test_directory_entries_before_files_are_skipped_not_the_end(tmp_path: Path) -> None:
    """Real archives list their directories first (Zenodo's Codeml.zip: ``Codeml/``)."""
    z = _zip(tmp_path / "d.zip", {"top/a.txt": b"a", "top/b.txt": b"b"}, dirs=("top/",))
    t = _tar(
        tmp_path / "d.tar",
        [
            _typed("top", tarfile.DIRTYPE),
            _typed("top/fifo", tarfile.FIFOTYPE),
            ("top/a.txt", b"a"),
            ("top/b.txt", b"b"),
        ],
    )
    for arc, dest in ((z, tmp_path / "z"), (t, tmp_path / "t")):
        assert _rel(archive.extract_archive(arc, dest, max_bytes=100), dest) == [
            "top/a.txt",
            "top/b.txt",
        ]
        assert _files(dest) == ["top/a.txt", "top/b.txt"]


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_nested_members_land_in_a_new_nested_or_existing_dest(tmp_path: Path, kind: str) -> None:
    members = {"x/y/one.txt": b"1", "x/y/two.txt": b"2", "x/three.txt": b"3"}
    arc = (
        _zip(tmp_path / "n.zip", members)
        if kind == "zip"
        else _tar(tmp_path / "n.tar", list(members.items()))
    )
    fresh = tmp_path / "new" / "deeper"
    got = archive.extract_archive(arc, fresh, max_bytes=100)
    assert got == [(fresh / n).resolve() for n in members]  # archive order, absolute
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "kept.txt").write_bytes(b"k")
    archive.extract_archive(arc, existing, max_bytes=100)
    assert _files(existing) == ["kept.txt", "x/three.txt", "x/y/one.txt", "x/y/two.txt"]
    assert (existing / "x/y/two.txt").read_bytes() == b"2"


def test_a_repeated_member_is_one_file_holding_the_last_copy(tmp_path: Path) -> None:
    t = _tar(tmp_path / "r.tar", [("a.txt", b"one"), ("b.txt", b"b"), ("a.txt", b"two!")])
    got = archive.extract_archive(t, tmp_path / "out", max_bytes=100)
    assert _rel(got, tmp_path / "out") == ["a.txt", "b.txt"]
    assert len(got) == 2
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"two!"


# --- the size bound ----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_the_bound_is_cumulative_inclusive_and_leaves_nothing(tmp_path: Path, kind: str) -> None:
    members = {"a.bin": b"a" * 300, "b.bin": b"b" * 300}
    arc = (
        _zip(tmp_path / "s.zip", members)
        if kind == "zip"
        else _tar(tmp_path / "s.tar", list(members.items()))
    )
    exact = archive.extract_archive(arc, tmp_path / "exact", max_bytes=600)
    assert _rel(exact, tmp_path / "exact") == ["a.bin", "b.bin"]
    with pytest.raises(
        FetchTooLargeError,
        match=_msg(f"[FetchTooLargeError] extracted size exceeds max_bytes=599 for {arc.name}"),
    ):
        archive.extract_archive(arc, tmp_path / "over", max_bytes=599)
    assert _files(tmp_path / "over") == []  # neither the whole a.bin nor the partial b.bin


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_a_member_is_read_at_most_64_kib_at_a_time(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compressed member can expand far past ``max_bytes``; reading it whole before
    counting would hold all of it in memory. Each read asks for at most 64 KiB."""
    big = {"big.bin": b"\0" * 300_000}
    arc = (
        _zip(tmp_path / "b.zip", big)
        if kind == "zip"
        else _tar(tmp_path / "b.tar.gz", list(big.items()), mode="w:gz")
    )
    cls = zipfile.ZipExtFile if kind == "zip" else tarfile.ExFileObject
    sizes: list[int | None] = []
    real_read = cls.read

    def spy(self, n=-1):  # type: ignore[no-untyped-def]
        sizes.append(n)
        return real_read(self, n)

    monkeypatch.setattr(cls, "read", spy)
    archive.extract_archive(arc, tmp_path / "out", max_bytes=300_000)
    assert (tmp_path / "out" / "big.bin").stat().st_size == 300_000
    assert sizes and set(sizes) == {1 << 16}
    assert len(sizes) == 6  # five chunks of 64 KiB cover 300,000 bytes, then the empty read


# --- real archives from a live Zenodo record -----------------------------------------

# zenodo:5518441 ("Large-scale genome sampling reveals unique immunity and metabolic
# adaptations in bats"), small analysis-script archives, md5-checked by fetch.
_RECORD = "zenodo:5518441"
_EXPECTED = {
    "Trinity.zip": [
        "Trinity/BLAST/README.txt",
        "Trinity/BLAST/Trinityrun.sh",
        "Trinity/BLAST/missing_gene.sh",
    ],
    "Genome_assembly.zip": ["Genome_assembly/Genome_assembly.sh"],
    "Ultrametric_tree.tgz": [
        "Ultrametric_tree/FigTree.tre",
        "Ultrametric_tree/README.txt",
        "Ultrametric_tree/baseml.ctl",
        "Ultrametric_tree/extract_cds_seq_modified.py",
        "Ultrametric_tree/mcmctree1.ctl",
        "Ultrametric_tree/mcmctree2.ctl",
        "Ultrametric_tree/ultrametric_tree.bash",
    ],
}


async def _fetch_real(dest: Path, *, extract: bool) -> tuple[Path, list[Path]]:
    """Fetch the record's G*/T*/U* files; return the record's directory and the paths."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resource = await router.resolve(client, _RECORD)
        out = await fetch_mod.fetch_files(
            client, resource, dest=str(dest), files="[GTU]*", max_bytes=5_000_000, extract=extract
        )
    paths = [Path(p).resolve() for p in out.paths]
    root = next(p.parent for p in paths if p.name == "Trinity.zip")
    return root, paths


@pytest.fixture(scope="module")
def real_archives(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The record's archives as fetched (md5-checked), downloaded once for the module."""
    root, _ = asyncio.run(_fetch_real(tmp_path_factory.mktemp("dl"), extract=False))
    return root


@live_only
async def test_live_zenodo_zip_and_tgz_extract_every_member(tmp_path: Path) -> None:
    root, paths = await _fetch_real(tmp_path, extract=True)
    rel = sorted(p.relative_to(root).as_posix() for p in paths)
    want = sorted([*_EXPECTED, *(m for ms in _EXPECTED.values() for m in ms)])
    assert rel == want
    assert (root / "Genome_assembly/Genome_assembly.sh").read_bytes().startswith(b"#")


@live_only
def test_live_a_damaged_real_zip_is_refused_beside_the_intact_one(
    tmp_path: Path, real_archives: Path
) -> None:
    intact = real_archives / "Trinity.zip"
    got = archive.extract_archive(intact, tmp_path / "ok", max_bytes=1_000_000)
    assert _rel(got, tmp_path / "ok") == _EXPECTED["Trinity.zip"]

    with zipfile.ZipFile(intact) as zf:
        info = zf.getinfo("Trinity/BLAST/missing_gene.sh")
    raw = bytearray(intact.read_bytes())
    # Flip one byte of the member's compressed data (past its local header and name).
    at = info.header_offset + 30 + len(info.orig_filename) + len(info.extra) + 10
    raw[at] ^= 0xFF
    damaged = tmp_path / "Damaged.zip"
    damaged.write_bytes(bytes(raw))
    with pytest.raises(
        UpstreamUnavailableError,
        match=_unreadable("Damaged.zip", zlib.error),  # the inflater meets the flipped byte
    ):
        archive.extract_archive(damaged, tmp_path / "out", max_bytes=1_000_000)
    assert _files(tmp_path / "out") == []


@live_only
def test_live_a_tar_ending_in_a_real_zip_extracts_as_the_tar(
    tmp_path: Path, real_archives: Path
) -> None:
    tgz, zipped = real_archives / "Ultrametric_tree.tgz", real_archives / "Trinity.zip"
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(tgz, arcname=tgz.name)
        tf.add(zipped, arcname=zipped.name)
    assert zipfile.is_zipfile(bundle)

    got = archive.extract_archive(bundle, tmp_path / "out", max_bytes=1_000_000)
    assert _rel(got, tmp_path / "out") == ["Trinity.zip", "Ultrametric_tree.tgz"]
    assert (tmp_path / "out" / "Trinity.zip").read_bytes() == zipped.read_bytes()
