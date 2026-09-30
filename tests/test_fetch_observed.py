"""Fetch behaviour the nightly mutation run showed no test observed (#88).

Each test pins something a caller relies on and a surviving mutant could change
without any test failing: where a record's files land on disk, what is skipped and
why, the integrity checks a resume relies on, the byte accounting, and the exact
text of every error and warning.
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp.errors import (
    FetchTooLargeError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from data_aggregator_mcp.models import DataResource, FileEntry


def _record(*files: FileEntry, id_: str = "zenodo:1") -> DataResource:
    return DataResource(id=id_, source="zenodo", kind="dataset", title="t", files=list(files))


def _entry(name: str, url: str | None = None, **kw) -> FileEntry:
    return FileEntry(name=name, url=url if url is not None else f"https://h/{name}", **kw)


def _serving(bodies: dict[str, bytes], seen: list[httpx.Request] | None = None):
    """A client serving ``bodies`` by URL (404 otherwise), recording each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = bodies.get(str(request.url))
        return httpx.Response(404) if body is None else httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _tar_gz(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- where files land -----------------------------------------------------------------


def test_a_file_where_a_directory_is_planned_is_renamed() -> None:
    planned = fetch_mod._plan_paths(
        [_entry("data/x.csv", "https://h/1"), _entry("DATA", "https://h/2")]
    )
    assert [p.as_posix() for p in planned] == ["data/x.csv", "DATA~82476a16"]


def test_a_clash_below_the_top_directory_renames_that_directory_by_its_full_path() -> None:
    planned = fetch_mod._plan_paths(
        [
            _entry("a/Sub", "https://h/1"),
            _entry("a/sub/x.csv", "https://h/2"),
            _entry("a/other.csv", "https://h/3"),  # positive control: `a` itself is free
        ]
    )
    # The tag hashes "a/sub|1": the directory's whole path, not just its own name.
    assert [p.as_posix() for p in planned] == ["a/Sub", "a/sub~28ebbfa3/x.csv", "a/other.csv"]


def test_a_rename_that_is_itself_taken_tries_the_next_tag() -> None:
    """Tags are sha1(url|n)[:8] for n = 1, 2, ...: stable across runs, so resume still
    finds a renamed file, and distinct for each attempt."""
    entries = [
        _entry("Lung.h5ad", "https://h/l1"),
        _entry("Lung.h5ad", "https://h/l2"),  # takes tag n=1
        _entry("lung~bd648524.h5ad", "https://h/l3x"),  # occupies l3's n=1 name
        _entry("lung.h5ad", "https://h/l3"),  # clashes twice: n=1 taken, n=2 free
    ]
    assert [p.as_posix() for p in fetch_mod._plan_paths(entries)] == [
        "Lung.h5ad",
        "Lung~7f44cf9e.h5ad",
        "lung~bd648524.h5ad",
        "lung~0b21fff0.h5ad",
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a\\b\\c.csv", "a/b/c.csv"),
        ("..\\..\\evil.sh", "evil.sh"),
        ("/abs//x.csv", "abs/x.csv"),
        ("./a/./b.csv", "a/b.csv"),
        ("/", None),
        (".", None),
        ("./", None),
        ("", None),
        ("../..", None),
    ],
)
def test_a_file_name_is_scrubbed_to_a_path_inside_the_record_dir(
    name: str, expected: str | None
) -> None:
    rel = fetch_mod._relative_path(name)
    assert (rel.as_posix() if rel is not None else None) == expected


async def test_a_resource_id_cannot_climb_out_of_the_cache_dir(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    body = {"https://h/d.csv": b"d"}
    async with _serving(body) as client:
        climbing = await fetch_mod.fetch_files(
            client, _record(_entry("d.csv"), id_="dataone:../../escape"), dest=str(cache)
        )
        plain = await fetch_mod.fetch_files(client, _record(_entry("d.csv")), dest=str(cache))
    assert climbing.paths == [str(cache / "dataone" / "_" / "_" / "escape" / "d.csv")]
    assert plain.paths == [str(cache / "zenodo" / "1" / "d.csv")]
    assert not (tmp_path / "escape").exists()


async def test_unfetchable_entries_are_skipped_by_name_and_the_rest_still_land(
    tmp_path: Path,
) -> None:
    """An entry with no URL and one whose name scrubs to nothing come first in the
    manifest, so a skip that ended the loop would lose the file after them."""
    r = _record(
        _entry("/", "https://h/root"),
        _entry("nourl.csv", url=""),
        _entry("a/b/c.csv", "https://h/c"),  # two directory levels below the record dir
    )
    async with _serving({"https://h/c": b"c", "https://h/root": b"r"}) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))
    assert out.skipped == ["/", "nourl.csv"]
    assert out.paths == [str(tmp_path / "zenodo" / "1" / "a" / "b" / "c.csv")]
    assert (out.resumed, out.unverified, out.bytes) == ([], [], 1)


# --- integrity ------------------------------------------------------------------------


async def test_resume_redownloads_a_file_it_cannot_verify_or_that_fails_its_checksum(
    tmp_path: Path,
) -> None:
    """Only a verified file is resumed: an unsupported algorithm, or a same-size file
    whose checksum differs, is fetched again."""
    good = b"0123456789"
    stale = b"9876543210"  # same size, different bytes
    target = tmp_path / "zenodo" / "1"
    target.mkdir(parents=True)
    md5 = "md5:781e5e245d69b566979b86e28d23f2c7"  # of `good`
    cases = {
        "unsupported.bin": ("dandi-etag:abc", good),
        "stale.bin": (md5, stale),
        "fresh.bin": (md5, good),  # positive control: verified, so resumed
    }
    for name, (_, on_disk) in cases.items():
        (target / name).write_bytes(on_disk)
    r = _record(*(_entry(n, checksum=c, size=len(good)) for n, (c, _) in cases.items()))
    seen: list[httpx.Request] = []
    async with _serving({f"https://h/{n}": good for n in cases}, seen) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))
    assert sorted(str(q.url) for q in seen) == ["https://h/stale.bin", "https://h/unsupported.bin"]
    assert out.resumed == ["fresh.bin"]
    assert all((target / n).read_bytes() == good for n in cases)


async def test_a_hyphenated_algorithm_name_is_computed_not_skipped(tmp_path: Path) -> None:
    body = b"payload"
    sha256 = "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"
    good = _record(_entry("d.bin", checksum=f"SHA-256:{sha256}"))
    bad = _record(_entry("d.bin", checksum="SHA-256:" + "0" * 64))
    async with _serving({"https://h/d.bin": body}) as client:
        out = await fetch_mod.fetch_files(client, good, dest=str(tmp_path / "ok"))
        with pytest.raises(UpstreamUnavailableError) as mismatch:
            await fetch_mod.fetch_files(client, bad, dest=str(tmp_path / "bad"))
    assert out.unverified == []  # verified: SHA-256 is sha256
    assert str(mismatch.value) == "[UpstreamUnavailableError] checksum mismatch for d.bin"


async def test_an_unverifiable_checksum_is_logged_with_the_file_and_the_claim(
    tmp_path: Path, caplog
) -> None:
    r = _record(_entry("d.bin", checksum="dandi-etag:abc-1"))
    async with _serving({"https://h/d.bin": b"x"}) as client:
        with caplog.at_level(logging.WARNING, logger=fetch_mod.__name__):
            await fetch_mod.fetch_files(client, r, dest=str(tmp_path))
    assert [m.getMessage() for m in caplog.records if m.name == fetch_mod.__name__] == [
        "fetch d.bin: record declares checksum 'dandi-etag:abc-1' but its algorithm is not "
        "available; downloading WITHOUT verification"
    ]


@pytest.mark.parametrize(
    "page",
    [b"  \n<!DOCTYPE html><p>sign in", b"<HTML><body>paywall", b"\t<html lang=en>"],
)
async def test_an_html_page_served_for_a_pdf_is_refused(tmp_path: Path, page: bytes) -> None:
    bodies = {"https://h/paper.pdf": page, "https://h/real.pdf": b"%PDF-1.7\n<html> in a pdf"}
    async with _serving(bodies) as client:
        with pytest.raises(UpstreamUnavailableError) as refused:
            await fetch_mod.fetch_files(
                client, _record(_entry("paper.pdf", mime="application/pdf")), dest=str(tmp_path)
            )
        real = await fetch_mod.fetch_files(
            client, _record(_entry("real.pdf", mime="application/pdf")), dest=str(tmp_path)
        )
    assert str(refused.value) == (
        "[UpstreamUnavailableError] fetch paper.pdf: body is HTML, not the declared "
        "application/pdf (the URL likely served a login/paywall/error page)"
    )
    assert not (tmp_path / "zenodo" / "1" / "paper.pdf").exists()  # the partial is removed
    assert real.paths == [str(tmp_path / "zenodo" / "1" / "real.pdf")]  # positive control


# --- accounting and limits ------------------------------------------------------------


async def test_bytes_count_every_chunk_of_every_file(tmp_path: Path) -> None:
    async def chunks():
        for part in (b"a" * 70_000, b"b" * 70_000, b"c" * 5):
            yield part

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/big.bin":
            return httpx.Response(200, content=chunks())
        return httpx.Response(200, content=b"12345")

    r = _record(_entry("big.bin"), _entry("small.bin"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await fetch_mod.fetch_files(client, r, dest=str(tmp_path))
    assert out.bytes == 140_005 + 5
    assert (tmp_path / "zenodo" / "1" / "big.bin").stat().st_size == 140_005


async def test_the_declared_size_limit_is_inclusive_and_unknown_sizes_count_as_zero(
    tmp_path: Path,
) -> None:
    body = b"x" * 10
    exact = _record(_entry("d.bin", size=10))
    unknown = _record(_entry("e1.bin", size=None), _entry("e2.bin", size=None))
    bodies = {"https://h/d.bin": body, "https://h/e1.bin": b"", "https://h/e2.bin": b""}
    async with _serving(bodies) as client:
        at_limit = await fetch_mod.fetch_files(client, exact, dest=str(tmp_path), max_bytes=10)
        empty = await fetch_mod.fetch_files(client, unknown, dest=str(tmp_path), max_bytes=1)
        with pytest.raises(FetchTooLargeError) as over:
            await fetch_mod.fetch_files(client, exact, dest=str(tmp_path), max_bytes=9)
    assert (at_limit.bytes, empty.bytes) == (10, 0)
    assert str(over.value) == (
        "[FetchTooLargeError] selected files total 10 bytes exceed max_bytes=9; "
        "pass force=true to override"
    )


async def test_force_lifts_the_download_limit_but_not_the_extraction_ceiling(
    tmp_path: Path,
) -> None:
    """With force, a stream longer than max_bytes completes, and an archive may extract
    up to max_bytes even after its download used the budget; beyond it, it stops."""
    small = _tar_gz("inner.bin", b"I" * 900)
    large = _tar_gz("inner.bin", b"I" * 1100)
    bodies = {
        "https://h/long.bin": b"L" * 2000,
        "https://h/small.tar.gz": small,
        "https://h/large.tar.gz": large,
    }
    async with _serving(bodies) as client:
        long = await fetch_mod.fetch_files(
            client,
            _record(_entry("long.bin")),
            dest=str(tmp_path / "a"),
            max_bytes=1000,
            force=True,
        )
        with pytest.raises(FetchTooLargeError) as unforced:
            await fetch_mod.fetch_files(
                client, _record(_entry("long.bin")), dest=str(tmp_path / "b"), max_bytes=1000
            )
        fits = await fetch_mod.fetch_files(
            client,
            _record(_entry("small.tar.gz")),
            dest=str(tmp_path / "c"),
            max_bytes=1000,
            force=True,
            extract=True,
        )
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client,
                _record(_entry("large.tar.gz")),
                dest=str(tmp_path / "d"),
                max_bytes=1000,
                force=True,
                extract=True,
            )
    assert long.bytes == 2000
    assert str(unforced.value) == (
        "[FetchTooLargeError] stream exceeded max_bytes while fetching long.bin"
    )
    assert any(p.endswith("inner.bin") for p in fits.paths)


async def test_an_archive_is_not_extracted_unless_asked(tmp_path: Path) -> None:
    arc = _tar_gz("inner.txt", b"inner")
    async with _serving({"https://h/bundle.tar.gz": arc}) as client:
        out = await fetch_mod.fetch_files(
            client, _record(_entry("bundle.tar.gz")), dest=str(tmp_path)
        )
    assert out.paths == [str(tmp_path / "zenodo" / "1" / "bundle.tar.gz")]


async def test_a_download_gets_its_own_timeout_not_the_clients(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    async with _serving({"https://h/d.bin": b"d"}, seen) as client:
        await fetch_mod.fetch_files(client, _record(_entry("d.bin")), dest=str(tmp_path))
    assert seen[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 300.0
    )


async def test_the_provenance_sidecar_is_indented_json(tmp_path: Path) -> None:
    async with _serving({"https://h/d.bin": b"d"}) as client:
        await fetch_mod.fetch_files(client, _record(_entry("d.bin")), dest=str(tmp_path))
    lines = (tmp_path / "zenodo" / "1" / ".dataresource.json").read_text().splitlines()
    assert lines[0] == "{" and lines[1].startswith('  "') and not lines[1].startswith("   ")


# --- error messages -------------------------------------------------------------------


async def test_http_and_transport_failures_name_the_file_and_the_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down.bin":
            raise httpx.ConnectError("refused")
        return httpx.Response(404 if request.url.path == "/gone.bin" else 503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        failures = {}
        for name, kind in (
            ("gone.bin", NotFoundError),
            ("busy.bin", UpstreamUnavailableError),
            ("down.bin", UpstreamUnavailableError),
        ):
            with pytest.raises(kind) as exc:
                await fetch_mod.fetch_files(client, _record(_entry(name)), dest=str(tmp_path))
            failures[name] = str(exc.value)
    assert failures == {
        "gone.bin": "[NotFoundError] fetch gone.bin → HTTP 404 (https://h/gone.bin)",
        "busy.bin": "[UpstreamUnavailableError] fetch busy.bin → HTTP 503 (https://h/busy.bin)",
        "down.bin": "[UpstreamUnavailableError] fetch down.bin transport failure: "
        "ConnectError('refused')",
    }


async def test_a_private_address_is_refused_by_file_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS", raising=False)
    async with _serving({}) as client:
        with pytest.raises(ValidationError) as refused:
            await fetch_mod.fetch_files(
                client, _record(_entry("d.bin", "http://127.0.0.1/d.bin")), dest=str(tmp_path)
            )
    assert str(refused.value).startswith(
        "[ValidationError] fetch d.bin: '127.0.0.1' resolves to the non-public address 127.0.0.1"
    )


async def test_nothing_to_fetch_says_why_and_lists_at_most_twenty_names(tmp_path: Path) -> None:
    names = [f"f{i:02}.csv" for i in range(21)]
    many = _record(*(_entry(n, url="") for n in names))
    twenty = _record(*(_entry(n) for n in names[:20]))
    async with _serving({}) as client:
        with pytest.raises(NotFoundError) as empty:
            await fetch_mod.fetch_files(client, _record(), dest=str(tmp_path))
        with pytest.raises(ValidationError) as unmatched_many:
            await fetch_mod.fetch_files(
                client, _record(*(_entry(n) for n in names)), dest=str(tmp_path), files="*.tsv"
            )
        with pytest.raises(ValidationError) as unmatched_twenty:
            await fetch_mod.fetch_files(client, twenty, dest=str(tmp_path), files="*.tsv")
        with pytest.raises(NotFoundError) as unfetchable:
            await fetch_mod.fetch_files(client, many, dest=str(tmp_path))
    first20 = ", ".join(names[:20])
    assert str(empty.value) == (
        "[NotFoundError] zenodo:1 lists no downloadable files (restricted, embargoed, or "
        "metadata-only record); nothing was fetched"
    )
    assert str(unmatched_many.value) == (
        f"[ValidationError] files='*.tsv' matched none of the 21 file(s) in zenodo:1: {first20} ..."
    )
    assert str(unmatched_twenty.value) == (
        f"[ValidationError] files='*.tsv' matched none of the 20 file(s) in zenodo:1: {first20}"
    )
    assert str(unfetchable.value) == (
        "[NotFoundError] zenodo:1: none of the 21 selected file(s) could be fetched "
        f"(no download URL or no usable file name): {first20}"
    )
