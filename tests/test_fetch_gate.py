from __future__ import annotations

import hashlib
import os

import httpx
import pytest

from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp import router, server
from data_aggregator_mcp.errors import FetchNotSupportedError, FetchTooLargeError
from data_aggregator_mcp.models import DataResource, FileEntry
from data_aggregator_mcp.server import _is_fetchable

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def test_is_fetchable_allows_zenodo_and_bare_digits() -> None:
    assert _is_fetchable("zenodo:7654321") is True
    assert _is_fetchable("123456") is True


def test_is_fetchable_rejects_unwired_sources() -> None:
    # An id with no allowlisted prefix (and not a bare Zenodo digit) has no backend.
    assert _is_fetchable("biostudies:S-BSST123") is False
    assert _is_fetchable("nonsense") is False


def test_is_fetchable_allows_datacite_prefix() -> None:
    # datacite: passes the cheap pre-gate; the precise repo check is post-resolve.
    assert _is_fetchable("datacite:10.7910/DVN/TJCLKP") is True


def test_is_fetchable_allows_sra() -> None:
    assert _is_fetchable("sra:SRX079566") is True


def test_is_fetchable_allows_geo() -> None:
    assert _is_fetchable("geo:GSE10072") is True


async def test_dispatch_fetch_routes_sra_through_router(httpx_mock, monkeypatch, tmp_path) -> None:
    content = b"fake fastq bytes"
    url = "https://ftp.example/SRR1_1.fastq.gz"
    resource = DataResource(
        id="sra:SRX079566",
        source="sra",
        kind="dataset",
        title="t",
        files=[
            FileEntry(
                name="SRR1_1.fastq.gz",
                size=len(content),
                url=url,
                checksum="md5:" + hashlib.md5(content).hexdigest(),
            )
        ],
    )
    called = {}

    async def fake_resolve(client, fid):
        called["fid"] = fid
        return resource

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    httpx_mock.add_response(url=url, content=content)
    out = await server._dispatch("fetch", {"id": "sra:SRX079566", "dest": str(tmp_path)})
    # Proves the fetch handler routes through router.resolve (not zenodo.resolve)
    # now that a non-Zenodo source is fetchable.
    assert called["fid"] == "sra:SRX079566"
    # _dispatch's fetch branch returns FetchResult.model_dump() -> {"paths", "bytes", "skipped"}.
    assert out["paths"]
    assert out["bytes"] == len(content)


async def test_dispatch_fetch_fails_loud_for_dryad_manifest_only(monkeypatch, tmp_path) -> None:
    async def fake_resolve(client, fid):
        return DataResource(
            id=fid,
            source="dryad",
            kind="dataset",
            title="t",
            doi="10.5061/dryad.x",
            files=[FileEntry(name="a", size=1, url="https://x/a", checksum=None)],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    with pytest.raises(FetchNotSupportedError, match="dryad"):
        await server._dispatch("fetch", {"id": "datacite:10.5061/dryad.x", "dest": str(tmp_path)})


async def test_dispatch_fetch_allows_osf_datacite(httpx_mock, monkeypatch, tmp_path) -> None:
    content = b"hello"

    async def fake_resolve(client, fid):
        return DataResource(
            id=fid,
            source="osf",
            kind="dataset",
            title="t",
            doi="10.17605/osf.io/x",
            files=[
                FileEntry(
                    name="a.csv",
                    size=len(content),
                    url="https://osf.example/a",
                    checksum="md5:" + hashlib.md5(content).hexdigest(),
                )
            ],
        )

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    httpx_mock.add_response(url="https://osf.example/a", content=content)
    out = await server._dispatch(
        "fetch", {"id": "datacite:10.17605/osf.io/x", "dest": str(tmp_path)}
    )
    assert out["paths"]


def test_literature_prefixes_are_fetchable() -> None:
    from data_aggregator_mcp import server

    assert server._is_fetchable("pubmed:23066504")
    assert server._is_fetchable("openaire:oai123")


def test_ensure_fulltext_available_raises_when_no_files() -> None:
    import pytest

    from data_aggregator_mcp import server
    from data_aggregator_mcp.errors import FetchNotSupportedError
    from data_aggregator_mcp.models import DataResource

    paywalled = DataResource(id="pubmed:1", source="pubmed", kind="publication", title="t")
    with pytest.raises(FetchNotSupportedError, match="open-access full text"):
        server._ensure_fulltext_available("pubmed:1", paywalled)


def test_ensure_fulltext_available_passes_when_files_present() -> None:
    from data_aggregator_mcp import server
    from data_aggregator_mcp.models import DataResource, FileEntry

    oa = DataResource(
        id="pubmed:1",
        source="pubmed",
        kind="publication",
        title="t",
        files=[FileEntry(name="PMC9.xml", url="http://x/ft", source="europepmc")],
    )
    server._ensure_fulltext_available("pubmed:1", oa)  # no raise


@live_only
async def test_live_sra_fetch_smallest_fastq_verifies_md5(tmp_path) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resource = await router.resolve(client, "sra:SRX079566")
        assert resource.files, "SRA resolve should attach an ENA FASTQ manifest"
        smallest = min(resource.files, key=lambda f: f.size or 1 << 62)
        assert smallest.checksum and smallest.checksum.startswith("md5:")
        out = await fetch_mod.fetch_files(
            client,
            resource,
            dest=str(tmp_path),
            files=smallest.name,
            max_bytes=300_000_000,
        )
    assert out.paths and out.bytes > 0
    # fetch_files raises on checksum mismatch, so reaching here means md5 verified.


@live_only
async def test_live_geo_fetch_small_suppl_file(tmp_path) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resource = await router.resolve(client, "geo:GSE10072")
        names = {f.name for f in resource.files}
        assert "filelist.txt" in names, "GEO resolve should attach suppl files"
        out = await fetch_mod.fetch_files(
            client,
            resource,
            dest=str(tmp_path),
            files="filelist.txt",
            max_bytes=10_000_000,
        )
    assert out.paths and out.bytes > 0  # downloads despite checksum=None (unverified)


@live_only
async def test_live_sra_fetch_refuses_over_max_bytes(tmp_path) -> None:
    # SRA files carry declared sizes -> fail-loud BEFORE downloading (pre-flight).
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resource = await router.resolve(client, "sra:SRX079566")
        assert resource.files
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client,
                resource,
                dest=str(tmp_path),
                max_bytes=1024,
                force=False,
            )


@live_only
async def test_live_geo_fetch_refuses_over_max_bytes_midstream(tmp_path) -> None:
    # GEO suppl files have no declared size -> the mid-stream guard catches it
    # quickly (after ~max_bytes bytes), so use a tiny cap on the large RAW.tar.
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resource = await router.resolve(client, "geo:GSE10072")
        raw = next(f for f in resource.files if f.name == "GSE10072_RAW.tar")
        with pytest.raises(FetchTooLargeError):
            await fetch_mod.fetch_files(
                client,
                resource,
                dest=str(tmp_path),
                files=raw.name,
                max_bytes=4096,
                force=False,
            )


def test_zenodo_is_datacite_fetchable() -> None:
    from data_aggregator_mcp import server

    assert "zenodo" in server._DATACITE_FETCHABLE
