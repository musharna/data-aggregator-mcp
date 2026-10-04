from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import figshare

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_ARTICLE = {
    "id": 31375579,
    "doi": "10.6084/m9.figshare.31375579.v2",
    "files": [
        {
            "name": "small.csv",
            "size": 123,
            "is_link_only": False,
            "download_url": "https://ndownloader.figshare.com/files/111",
            "computed_md5": "abc123",
        },
        {
            "name": "ext_link",
            "size": 0,
            "is_link_only": True,
            "download_url": "https://ndownloader.figshare.com/files/222",
            "computed_md5": None,
        },
    ],
}


def test_article_id_from_doi() -> None:
    assert figshare._article_id("10.6084/m9.figshare.31375579") == "31375579"
    assert figshare._article_id("10.6084/m9.figshare.31375579.v2") == "31375579"
    assert figshare._article_id("10.5061/dryad.x") is None


async def test_files_parses_article(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.figshare.com/v2/articles/31375579/versions/2", json=_ARTICLE
    )
    async with httpx.AsyncClient() as client:
        files = await figshare.files(client, "10.6084/m9.figshare.31375579.v2")
    assert {f.name for f in files} == {"small.csv"}  # is_link_only dropped
    f = files[0]
    assert f.url == "https://ndownloader.figshare.com/files/111"
    assert f.size == 123
    assert f.checksum == "md5:abc123"


async def test_figshare_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://api.figshare.com/v2/articles/31375579/versions/2",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await figshare.files(client, "10.6084/m9.figshare.31375579.v2")


async def test_a_withdrawn_article_is_not_found_and_an_outage_is_not(
    httpx_mock, monkeypatch
) -> None:
    """A withdrawn article answers 404 for every version (32732757, live 2026-10-04): the
    lister says so by name, as NotFoundError, so DataCite resolve can keep the record. A
    503 past its retries is an outage and must not read as withdrawn."""
    from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    base = "https://api.figshare.com/v2/articles"
    httpx_mock.add_response(
        url=f"{base}/32732757/versions/1",
        status_code=404,
        json={"message": "Entity not found: ArticleVersion", "code": "EntityNotFound"},
    )
    httpx_mock.add_response(url=f"{base}/32732757", status_code=404)
    for _ in range(3):
        httpx_mock.add_response(url=f"{base}/1234", status_code=503)
    httpx_mock.add_response(url=f"{base}/31375579/versions/2", json=_ARTICLE)
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            NotFoundError,
            match=r"^\[NotFoundError\] Figshare answers 404 for article 32732757 version 1: "
            r"withdrawn or removed upstream$",
        ):
            await figshare.files(client, "10.6084/m9.figshare.32732757.v1")
        with pytest.raises(NotFoundError, match=r"article 32732757: withdrawn"):
            await figshare.files(client, "10.6084/m9.figshare.32732757")
        with pytest.raises(UpstreamUnavailableError, match=r"last HTTP 503"):
            await figshare.files(client, "10.6084/m9.figshare.1234")
        # positive control: a served article still lists its files
        listed = await figshare.files(client, "10.6084/m9.figshare.31375579.v2")
    assert [f.name for f in listed] == ["small.csv"]


@live_only
async def test_live_figshare_manifest_has_md5(monkeypatch) -> None:
    # Figshare's smallest sample file is ~123 MB → manifest-only check, no download.
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await figshare.files(client, "10.6084/m9.figshare.31375579")
    assert files
    assert any(f.checksum and f.checksum.startswith("md5:") for f in files)


def test_article_id_from_institutional_figshare_doi() -> None:
    """Audit 2026-09-22 H1: institutional Figshare portals mint DOIs under their own
    prefix without the literal "figshare." (real, captured live 2026-09-22:
    10.25405/ncl.33951526.v1 → api.figshare.com/v2/articles/33951526). The parser
    returned None, so the record resolved with files=[] and fetch "succeeded" empty."""
    assert figshare._article_id("10.25405/ncl.33951526.v1") == "33951526"
    assert figshare._article_id("10.25405/ncl.33951526") == "33951526"
    assert figshare._article_id("10.6084/m9.figshare.31375579.v2") == "31375579"  # control
    assert figshare._article_id("10.5061/dryad.x") is None


async def test_files_institutional_doi_lists_files_and_rejects_mismatch(httpx_mock) -> None:
    """The parsed id must name the SAME article: a DOI-shaped number that happens to be
    some other article's id (the global id space is shared) must not attach that
    article's files."""
    ours = dict(_ARTICLE, id=33951526, doi="10.25405/ncl.33951526.v1")
    httpx_mock.add_response(url="https://api.figshare.com/v2/articles/33951526", json=ours)
    other = dict(_ARTICLE, id=9918287, doi="10.1021/acs.orglett.9b03122.s001")
    httpx_mock.add_response(
        url="https://api.figshare.com/v2/articles/9918287/versions/1", json=other
    )
    async with httpx.AsyncClient() as client:
        files = await figshare.files(client, "10.25405/ncl.33951526")
        assert {f.name for f in files} == {"small.csv"}
        assert await figshare.files(client, "10.25405/data.ncl.9918287.v1") == []


def _fs_file(name: str, fid: int, md5: str) -> dict:
    return {
        "name": name,
        "size": 10,
        "is_link_only": False,
        "download_url": f"https://ndownloader.figshare.com/files/{fid}",
        "computed_md5": md5,
    }


async def test_versioned_doi_lists_that_versions_files_not_the_latest(httpx_mock) -> None:
    """A-H2 (audit 2026-09-27, shapes captured live): 10.6084/m9.figshare.32732757.v1 has
    one file (Data_Icarus.xlsx); /articles/32732757 is v3 (two other files). The version
    suffix was stripped before the request AND on both sides of the DOI check, so a v1
    DOI fetched v3's bytes with v3's md5s — verified, and wrong."""
    base = "https://api.figshare.com/v2/articles/32732757"
    v3 = {
        "id": 32732757,
        "doi": "10.6084/m9.figshare.32732757.v3",
        "files": [_fs_file("Supplementary_Table_S1.xlsx", 69381495, "3abb")],
    }
    v1 = dict(
        v3,
        doi="10.6084/m9.figshare.32732757.v1",
        files=[_fs_file("Data_Icarus.xlsx", 65685420, "c9eb")],
    )
    httpx_mock.add_response(url=f"{base}/versions/1", json=v1)
    httpx_mock.add_response(url=base, json=v3)
    async with httpx.AsyncClient() as client:
        files = await figshare.files(client, "10.6084/m9.figshare.32732757.v1")
        assert [(f.name, f.checksum) for f in files] == [("Data_Icarus.xlsx", "md5:c9eb")]
        # positive control: an unversioned DOI still means the current version
        latest = await figshare.files(client, "10.6084/m9.figshare.32732757")
    assert [f.name for f in latest] == ["Supplementary_Table_S1.xlsx"]


@live_only
async def test_live_versioned_doi_gets_that_versions_files() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        # 32732757 (used until 2026-09-28) was withdrawn upstream: the public API now
        # answers 404 for every version. 28283042's v1 file differs from its latest.
        v1 = await figshare.files(client, "10.6084/m9.figshare.28283042.v1")
        # positive control: the unversioned DOI is the latest version, a different file
        latest = await figshare.files(client, "10.6084/m9.figshare.28283042")
    assert [f.name for f in v1] == ["Barahona-Segovia et al 2025 supplementary.xlsx"]
    assert v1[0].checksum == "md5:c1ca06e9d3e7539bc3b506a6ea735bf9"
    assert [f.name for f in latest] != [f.name for f in v1]
