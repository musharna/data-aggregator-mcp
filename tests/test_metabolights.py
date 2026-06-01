import os

import httpx
import pytest

from data_aggregator_mcp import metabolights

# Apache autoindex listing as the EBI FTP mirror actually serves it: a sort link,
# an absolute parent link, subdirectories, and the real (FTP) filenames — note the
# assay file is `a_MTBLS1_metabolite_profiling…`, NOT the WS API's `…_NMR_…`
# variant that 404s. Sourcing names from this listing is the whole point of the fix.
_LISTING = """<html><head><title>Index</title></head><body>
<h1>Index of /pub/databases/metabolights/studies/public/MTBLS1</h1>
<table>
<tr><td><a href="?C=N;O=D">Name</a></td></tr>
<tr><td><a href="/pub/databases/metabolights/studies/public/">Parent Directory</a></td></tr>
<tr><td><a href="FILES/">FILES/</a></td></tr>
<tr><td><a href="HASHES/">HASHES/</a></td></tr>
<tr><td><a href="a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt">a_</a></td></tr>
<tr><td><a href="i_Investigation.txt">i_Investigation.txt</a></td></tr>
<tr><td><a href="s_MTBLS1.txt">s_MTBLS1.txt</a></td></tr>
</table></body></html>"""


@pytest.mark.asyncio
async def test_files_from_ftp_listing_real_names_skip_subdirs():
    async def handler(request):
        assert request.url.path.endswith("/public/MTBLS1/")
        return httpx.Response(200, text=_LISTING)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await metabolights.files(c, "MTBLS1")
    # Subdirs (FILES/, HASHES/), the sort link (?C=…) and the absolute parent link
    # are all excluded; only real top-level files survive, with their FTP names.
    assert [f.name for f in files] == [
        "a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt",
        "i_Investigation.txt",
        "s_MTBLS1.txt",
    ]
    assert files[0].url == (
        "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/"
        "MTBLS1/a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt"
    )
    assert all(f.checksum is None and f.size is None for f in files)
    assert all(f.source == "metabolights" for f in files)


def test_listing_files_filters_sort_links_parent_and_dirs():
    html = (
        '<a href="?C=N;O=D">x</a><a href="/parent/">p</a>'
        '<a href="SUBDIR/">d</a><a href="real_file.tsv">f</a>'
    )
    assert metabolights._listing_files(html) == ["real_file.tsv"]


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_metabolights_files_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await metabolights.files(c, "MTBLS1")
        assert files
        head = await c.head(files[0].url)
        assert head.status_code < 400
