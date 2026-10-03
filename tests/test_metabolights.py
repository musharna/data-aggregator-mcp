import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import metabolights
from data_aggregator_mcp.errors import NotFoundError

# The Apache autoindex page the EBI FTP mirror serves for MTBLS1, verbatim (captured
# 2026-10-02): sort links, the absolute parent link, three subdirectories and the real
# (FTP) filenames — the assay file is `a_MTBLS1_metabolite_profiling…`, NOT the WS API's
# `…_NMR_…` variant that 404s. Sourcing names from this listing is the whole point.
_LISTING = (Path(__file__).parent / "fixtures" / "metabolights_MTBLS1_index.html").read_text()


@pytest.mark.asyncio
async def test_files_from_ftp_listing_real_names_skip_subdirs():
    async def handler(request):
        if request.url.path.endswith("/HASHES/metadata_sha256.json"):
            return httpx.Response(404)  # a study that publishes no hashes
        assert request.url.path.endswith("/public/MTBLS1/")
        return httpx.Response(200, text=_LISTING)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await metabolights.files(c, "MTBLS1")
    # Subdirs (FILES/, HASHES/, METADATA_REVISIONS/), the sort links (?C=…) and the
    # absolute parent link are all excluded; only top-level files survive, with their
    # FTP names.
    assert [f.name for f in files] == [
        "a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt",
        "i_Investigation.txt",
        "m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv",
        "s_MTBLS1.txt",
    ]
    assert files[0].url == (
        "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/"
        "MTBLS1/a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt"
    )
    assert all(f.checksum is None and f.size is None for f in files)
    assert all(f.source == "metabolights" for f in files)


_A_SHA = "cd615abd1532ae3ba70efb35681710a6de985aef1c03a836ec2c3c824cffde7a"
_I_SHA = "2cd04c9ff1f252d15d1638355b958363adec535acf3ba7d8f118f2514bf20d1d"


@pytest.mark.asyncio
async def test_files_carry_the_sha256_metabolights_publishes():
    """B-M6 (audit 2026-09-27): every study publishes HASHES/metadata_sha256.json
    (verified live: MTBLS1's entries equal the sha256 of the served bytes), but the
    manifest set checksum=None, so fetch could not verify a download. A file the hash
    list omits stays unverified rather than getting a made-up checksum."""
    hashes = {  # m_ and s_MTBLS1.txt deliberately absent
        "a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt": _A_SHA,
        "i_Investigation.txt": _I_SHA,
    }

    async def handler(request):
        if request.url.path.endswith("/MTBLS1/HASHES/metadata_sha256.json"):
            return httpx.Response(200, json=hashes)
        return httpx.Response(200, text=_LISTING)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await metabolights.files(c, "MTBLS1")
    assert {f.name: f.checksum for f in files} == {
        "a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt": f"sha256:{_A_SHA}",
        "i_Investigation.txt": f"sha256:{_I_SHA}",
        "m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv": None,
        "s_MTBLS1.txt": None,
    }


def test_listing_files_filters_sort_links_parent_and_dirs():
    html = (
        "<title>Index of /pub/databases/metabolights/studies/public/MTBLS9</title>"
        '<a href="?C=N;O=D">x</a><a href="/parent/">p</a>'
        '<a href="SUBDIR/">d</a><a href="real_file.tsv">f</a>'
    )
    assert metabolights._listing_files(html, "MTBLS9") == ["real_file.tsv"]


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


@_live_only
@pytest.mark.asyncio
async def test_live_metabolights_checksum_matches_the_served_bytes():
    """B-M6 live: the attached sha256 is the hash of the bytes the listed URL serves."""
    import hashlib

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await metabolights.files(c, "MTBLS1")
        inv = next(f for f in files if f.name == "i_Investigation.txt")
        body = (await c.get(inv.url)).content
    assert inv.checksum == f"sha256:{hashlib.sha256(body).hexdigest()}"


@_live_only
@pytest.mark.asyncio
@pytest.mark.parametrize("acc", ["MTBLS1", "MTBLS8", "MTBLS16"])
async def test_live_a_study_index_is_read_and_its_names_are_served(acc):
    """The index check accepts the live page (25 of 25 sampled 2026-10-02 pass it) and
    every name read from it is a file the mirror serves under the URL built for it."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await metabolights.files(c, acc)
        names = {f.name for f in files}
        assert {"i_Investigation.txt", f"s_{acc}.txt"} <= names
        study = next(f for f in files if f.name == f"s_{acc}.txt")
        assert study.checksum is not None and study.checksum.startswith("sha256:")
        assert (await c.head(study.url)).status_code == 200


@_live_only
@pytest.mark.asyncio
async def test_live_an_unknown_study_is_not_found_and_a_malformed_one_is_not_asked():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] MetaboLights files → HTTP 404"
        ):
            await metabolights.files(c, "MTBLS999999999")
        # On the old code this listed the files of MTBLS1's HASHES/ directory as a study's.
        with pytest.raises(
            NotFoundError,
            match=r"^\[NotFoundError\] not a MetaboLights study accession: 'MTBLS1/HASHES'$",
        ):
            await metabolights.files(c, "MTBLS1/HASHES")
        assert await metabolights.files(c, "MTBLS1")  # positive control
