"""Pin the request ``geo.supplementary_files`` sends and the entries it reads back."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import geo
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

_SUPPL = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/suppl/"


@pytest.mark.parametrize(
    "ftplink",
    [
        "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/",
        "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072",
        "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072//",
        "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/",
    ],
)
def test_the_listing_is_the_suppl_directory_over_https(ftplink: str) -> None:
    assert geo._suppl_url(ftplink) == _SUPPL


def test_only_the_scheme_is_rewritten() -> None:
    assert geo._suppl_url("ftp://h/a/ftp://b/") == "https://h/a/ftp://b/suppl/"
    assert geo._suppl_url("ftp://h/GSE1X/") == "https://h/GSE1X/suppl/"  # only slashes trimmed


async def test_the_listing_request_and_the_entries_read_from_it(httpx_mock: HTTPXMock) -> None:
    html = (
        '<pre><a href="?C=N;O=D">Name</a>\n'
        '<a href="/geo/series/GSE10nnn/GSE10072/">Parent Directory</a>\n'
        '<a href="http://www.ncbi.nlm.nih.gov/geo/">GEO</a>\n'
        '<a href="https://www.hhs.gov/vulnerability-disclosure-policy/index.html">HHS</a>\n'
        '<a href="GSE10072_RAW.tar">GSE10072_RAW.tar</a>   2013-01-17 13:59  375M\n'
        '<a  href="filelist.txt" title="x">filelist.txt</a>\n'
        '<A HREF="upper.txt">upper.txt</A></pre>'
    )
    httpx_mock.add_response(url=_SUPPL, method="GET", text=html)
    async with httpx.AsyncClient() as client:
        files = await geo.supplementary_files(
            client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/"
        )
    assert files == [
        FileEntry(name="GSE10072_RAW.tar", url=_SUPPL + "GSE10072_RAW.tar"),
        FileEntry(name="filelist.txt", url=_SUPPL + "filelist.txt"),
    ]
    assert all(f.size is None and f.checksum is None and f.mime is None for f in files)
    (req,) = httpx_mock.get_requests()
    assert (req.method, str(req.url)) == ("GET", _SUPPL)


async def test_a_listing_failure_names_the_geo_listing(httpx_mock: HTTPXMock, monkeypatch) -> None:
    from data_aggregator_mcp import _http

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", no_sleep)
    httpx_mock.add_response(url=_SUPPL, status_code=403, text="denied")
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GEO suppl listing → HTTP 403: denied$",
        ):
            await geo.supplementary_files(
                client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/"
            )
        httpx_mock.add_response(url=_SUPPL, status_code=404)  # positive control: no suppl/ dir
        assert (
            await geo.supplementary_files(
                client, "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE10nnn/GSE10072/"
            )
            == []
        )
