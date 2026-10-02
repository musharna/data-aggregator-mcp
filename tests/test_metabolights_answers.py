"""What the MetaboLights file lister makes of the directory pages the EBI mirror answers.

The listing is an Apache ``mod_autoindex`` page. Its grammar, from the httpd source
(``mod_autoindex.c`` ``output_directories``, ``util.c`` ``ap_os_escape_path``): each
entry's href is ``ap_escape_html(ap_os_escape_path(name, 0))`` — percent-escaped except
``$-_.+!*'(),:;@&=/~`` and alphanumerics, ``./`` put before a name whose first segment
holds a ``:``, then HTML-escaped — and a directory's name carries a trailing ``/``.
The page's title is ``Index of <the directory's path>``. 25 live study pages sampled
2026-10-02 (172 files) all match; none held a name that escaping changes, so the
escaped-name cases below are built with that grammar, not captured.
"""

import html
import re
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from data_aggregator_mcp import metabolights
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_PAGE = (Path(__file__).parent / "fixtures" / "metabolights_MTBLS1_index.html").read_text()
_NAMES = [
    "a_MTBLS1_metabolite_profiling_NMR_spectroscopy.txt",
    "i_Investigation.txt",
    "m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv",
    "s_MTBLS1.txt",
]
_LAST_ROW = (
    '<tr><td valign="top"><img src="/icons/text.gif" alt="[TXT]"></td><td><a href="s_MTBLS1.txt">'
)


def _with_entry(href: str) -> str:
    """The verbatim MTBLS1 page with one more file row, its href written as given."""
    assert _LAST_ROW in _PAGE
    row = f'<tr><td valign="top"></td><td><a href="{href}">x</a></td></tr>\n'
    return _PAGE.replace(_LAST_ROW, row + _LAST_ROW, 1)


def _apache_href(name: str) -> str:
    """The href mod_autoindex writes for a file called ``name``."""
    escaped = quote(name, safe="$-_.+!*'(),:;@&=/~")
    if ":" in name:
        escaped = "./" + escaped
    return html.escape(escaped, quote=False)


def _quoted(text: str) -> str:
    """``text`` as the error message quotes it (its repr), as a regex."""
    return re.escape(repr(text))


def _client(sent: list[httpx.Request], page: str, hashes: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/HASHES/metadata_sha256.json"):
            return httpx.Response(404) if hashes is None else httpx.Response(200, json=hashes)
        return httpx.Response(200, text=page)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _files(page: str, hashes: dict | None = None, acc: str = "MTBLS1"):
    sent: list[httpx.Request] = []
    async with _client(sent, page, hashes) as c:
        return await metabolights.files(c, acc)


# A 200 that is not the index of the study asked for. Read as a listing, the first two
# named files the study does not have (the page's own links) and the empty one named
# none; the third is another study's index.
_NOT_THE_INDEX = [
    pytest.param(
        "<html><head><title>EMBL-EBI</title></head><body><p>Service under maintenance.</p>"
        '<a href="https://www.ebi.ac.uk/about">About</a><a href="help.html">Help</a></body></html>',
        id="maintenance-page",
    ),
    pytest.param("", id="empty-body"),
    pytest.param(_PAGE.replace("public/MTBLS1", "public/MTBLS2"), id="another-study"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("page", _NOT_THE_INDEX)
async def test_a_page_that_is_not_the_study_index_is_upstream_trouble(page):
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] MetaboLights files: no directory index of MTBLS1 in ",
    ):
        await _files(page)
    # Positive control: the verbatim index is read.
    assert [f.name for f in await _files(_PAGE)] == _NAMES


# hrefs mod_autoindex never writes for an entry of this directory. On the old code each
# was listed as a file: the dot segments requested another directory of the mirror
# (`..` the parent, `../MTBLS2/…` another study), `?`/`#` a different resource.
_NOT_A_NAME = [
    "..",
    "../MTBLS2/i_Investigation.txt",
    "./..",
    "%2e%2e",
    "https://www.ebi.ac.uk/x.txt",
    "FILES/raw.zip",
    "x.txt?C=N",
    "x.txt#top",
    ".",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("href", _NOT_A_NAME)
async def test_an_entry_that_is_not_a_name_in_the_directory_is_refused(href):
    with pytest.raises(
        UpstreamUnavailableError,
        match=rf"^\[UpstreamUnavailableError\] MetaboLights files: {_quoted(href)} in the index of MTBLS1 is not a name in it$",
    ):
        await _files(_with_entry(href))
    # Positive control: the same page with a well-formed entry in that row is read.
    names = [f.name for f in await _files(_with_entry("x.txt"))]
    assert names == [*_NAMES[:3], "x.txt", _NAMES[3]]


# Names a study could hold that Apache escapes: a space, `#`, `%`, `?`, `&` (HTML-escaped),
# `:` (prefixed `./`), non-ASCII.
_ODD_NAMES = [
    "m_MTBLS1 v2 maf.tsv",
    "a#1.txt",
    "100%.txt",
    "why?.txt",
    "a&b.txt",
    "x:y.txt",
    "s_é.txt",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _ODD_NAMES)
async def test_a_listed_name_is_read_through_apache_escaping(name):
    """The name is the file's name, not its href: it is the key the study's hash list
    uses, and the URL requests exactly that file. On the old code the name was the raw
    href (`m_MTBLS1%20v2%20maf.tsv`), so it missed its sha256, and `./x:y.txt` kept the
    `./`."""
    assert _apache_href(name) != name  # the case exercises the escaping
    sha = "ab" * 32
    got = await _files(_with_entry(_apache_href(name)), hashes={name: sha})
    entry = next(f for f in got if f.name not in _NAMES)
    assert entry.name == name
    assert entry.checksum == f"sha256:{sha}"
    url = httpx.URL(entry.url)
    assert (url.scheme, url.host, url.query, url.fragment) == ("https", "ftp.ebi.ac.uk", b"", "")
    assert url.path == f"/pub/databases/metabolights/studies/public/MTBLS1/{name}"
    # Positive control: the plain names beside it are unchanged.
    assert [f.name for f in got if f.name in _NAMES] == _NAMES


# Accessions that are not a MetaboLights study's. The accession is the path segment of
# the directory URL, so each of these asked the mirror for another path on the old code.
_BAD_ACC = [
    "../MTBLS1",
    "MTBLS1/../MTBLS2",
    "MTBLS1/FILES",
    "MTBLS1?C=N",
    "MTBLS1#x",
    "mtbls1",
    "MTBLS",
    " MTBLS1",
    "MTBLS1\n",
    "REFMTBLS1",
    "",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("acc", _BAD_ACC)
async def test_a_malformed_accession_is_refused_before_the_network(acc):
    sent: list[httpx.Request] = []
    async with _client(sent, _PAGE) as c:
        with pytest.raises(
            NotFoundError,
            match=rf"^\[NotFoundError\] not a MetaboLights study accession: {_quoted(acc)}$",
        ):
            await metabolights.files(c, acc)
        assert sent == []
        # Positive control: a well-formed accession is asked for.
        assert [f.name for f in await metabolights.files(c, "MTBLS1")] == _NAMES
    assert [str(r.url) for r in sent][0] == (
        "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/MTBLS1/"
    )
