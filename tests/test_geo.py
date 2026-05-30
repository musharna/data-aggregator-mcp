from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import geo, omics

# Real GSE10072 suppl/ autoindex (captured live), trimmed.
_SUPPL_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html><head><title>Index of /geo/series/GSE10nnn/GSE10072/suppl</title></head>
<body><h1>Index of /geo/series/GSE10nnn/GSE10072/suppl</h1>
<pre>Name                           Last modified      Size  <hr><a href="/geo/series/GSE10nnn/GSE10072/">Parent Directory</a>                                    -
<a href="GSE10072_RAW.tar">GSE10072_RAW.tar</a>               2013-01-17 13:59  375M
<a href="filelist.txt">filelist.txt</a>                   2013-01-17 13:59  5.7K
<hr></pre>
<a href="https://www.hhs.gov/vulnerability-disclosure-policy/index.html">HHS Vulnerability Disclosure</a>
</body></html>"""


async def test_supplementary_files_parses_index(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/suppl/",
        text=_SUPPL_HTML,
    )
    async with httpx.AsyncClient() as client:
        files = await geo.supplementary_files(
            client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/"
        )
    names = {f.name for f in files}
    assert names == {"GSE10072_RAW.tar", "filelist.txt"}  # excludes parent dir + HHS link
    raw = next(f for f in files if f.name == "GSE10072_RAW.tar")
    assert raw.url == (
        "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/suppl/GSE10072_RAW.tar"
    )
    assert raw.checksum is None  # NCBI exposes no checksum for GEO suppl files


async def test_supplementary_files_empty_ftplink_returns_empty() -> None:
    async with httpx.AsyncClient() as client:
        assert await geo.supplementary_files(client, "") == []


async def test_supplementary_files_parses_anchor_with_extra_attrs(httpx_mock) -> None:
    html = (
        '<pre><a href="/parent/">Parent Directory</a>\n'
        '<a href="data.csv" class="file">data.csv</a></pre>'
    )
    httpx_mock.add_response(
        url="https://ftp.ncbi.nlm.nih.gov/geo/series/GSE9nnn/GSE9999/suppl/",
        text=html,
    )
    async with httpx.AsyncClient() as client:
        files = await geo.supplementary_files(
            client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE9nnn/GSE9999/"
        )
    assert {f.name for f in files} == {"data.csv"}


async def test_supplementary_files_404_returns_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://ftp.ncbi.nlm.nih.gov/geo/series/GSE9nnn/GSE9999/suppl/",
        status_code=404,
    )
    async with httpx.AsyncClient() as client:
        files = await geo.supplementary_files(
            client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE9nnn/GSE9999/"
        )
    assert files == []


async def test_resolve_geo_attaches_supplementary_files(httpx_mock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE10072[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {
                    "accession": "GSE10072",
                    "title": "Lung cancer",
                    "pdat": "2008/01/01",
                    "ftplink": "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/",
                },
            }
        },
    )
    httpx_mock.add_response(
        url="https://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/suppl/",
        text=_SUPPL_HTML,
    )
    async with httpx.AsyncClient() as client:
        r = await omics.resolve(client, "geo:GSE10072")
    assert r.id == "geo:GSE10072"
    assert {f.name for f in r.files} == {"GSE10072_RAW.tar", "filelist.txt"}


async def test_supplementary_files_skips_apache_sort_links(httpx_mock) -> None:
    html = (
        '<pre><a href="?C=N;O=D">Name</a> '
        '<a href="/parent/">Parent Directory</a> '
        '<a href="real_data.csv">real_data.csv</a></pre>'
    )
    httpx_mock.add_response(
        url="https://ftp.ncbi.nlm.nih.gov/geo/series/GSE8nnn/GSE8888/suppl/",
        text=html,
    )
    async with httpx.AsyncClient() as client:
        files = await geo.supplementary_files(
            client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE8nnn/GSE8888/"
        )
    assert {f.name for f in files} == {"real_data.csv"}


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_resolve_geo_attaches_supplementary_files() -> None:
    async with httpx.AsyncClient() as client:
        r = await omics.resolve(client, "geo:GSE10072")
    assert r.id == "geo:GSE10072"
    names = {f.name for f in r.files}
    assert {"GSE10072_RAW.tar", "filelist.txt"} <= names
