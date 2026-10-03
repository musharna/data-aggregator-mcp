"""Each Figshare DOI shape and article answer beside how the adapter must read it.

Probed live 2026-10-02 (api.figshare.com/v2 and api.datacite.org, anonymous):
- A collection DOI (``10.6084/m9.figshare.c.8708104.v1``; figshare.ars alone holds
  621,872 Collection DOIs) ends in a collection id. The adapter asked /articles for it,
  got ``404 {"message": "Entity not found: ArticleVersion"}``, and ``resolve`` of the
  DOI raised ``NotFoundError`` although DataCite holds it.
- Griffith's portal (DataCite client ``griffith.figshare``) mints ``10.57831/<id>``, the
  id after the prefix slash. The adapter found no id, so those records had no files;
  ``/articles/22306426`` lists two.
- An embargoed article answers ``"files": null`` with ``is_embargoed: true`` (3 of 3); a
  confidential one is also ``is_embargoed: true`` with null files. A metadata-only record
  is ``"files": []``. An article with no DOI answers ``"doi": ""`` (MMU 33549700).
  Re-probed 2026-10-03: the same embargoed and confidential articles (and 31046215,
  embargoed to 2050) now leave the ``files`` key out entirely, so ``resolve`` of every
  embargoed article raised a bare ``KeyError`` (nightly live run 37127895878).
- An md5 Figshare has not computed yet is ``"computed_md5": ""`` beside a supplied one
  (4 of 528 files; one is Griffith's video below).
- 52 article records and one version record (two pages of /articles, the shapes
  above, a 326-file figshare+ article): ``doi`` always a string, ``files`` a list or
  null-while-embargoed, every file's name, download_url and md5s strings, size an int,
  ``is_link_only`` a bool; the check refuses 0 of them. The 326-file list was complete (sizes sum to the article's
  ``size``), so the embedded list is not paged at that size.
"""

from __future__ import annotations

import copy
import logging

import httpx
import pytest

from data_aggregator_mcp import _http, datacite, figshare
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry
from tests.test_figshare import live_only


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


_API = "https://api.figshare.com/v2/articles"

# Verbatim from /articles/22306426 (Griffith), trimmed to the fields the adapter reads.
_GRIFFITH = {
    "id": 22306426,
    "doi": "10.57831/22306426",
    "is_embargoed": False,
    "files": [
        {
            "id": 39679450,
            "name": "Dear Alice_TheImmersiveGuitar_Titles.mp4",
            "size": 1260453055,
            "is_link_only": False,
            "download_url": "https://ndownloader.figshare.com/files/39679450",
            "supplied_md5": "25c1ea2494abfe66c45478eb9f0e85e9",
            "computed_md5": "",
        },
        {
            "id": 39693688,
            "name": "Karin_Vanessa_TIG.JPG",
            "size": 2685661,
            "is_link_only": False,
            "download_url": "https://ndownloader.figshare.com/files/39693688",
            "supplied_md5": "49f4237c802f067b9767c1de4b021999",
            "computed_md5": "49f4237c802f067b9767c1de4b021999",
        },
    ],
}
_GRIFFITH_FILES = [
    FileEntry(
        name="Dear Alice_TheImmersiveGuitar_Titles.mp4",
        size=1260453055,
        url="https://ndownloader.figshare.com/files/39679450",
        checksum="md5:25c1ea2494abfe66c45478eb9f0e85e9",
    ),
    FileEntry(
        name="Karin_Vanessa_TIG.JPG",
        size=2685661,
        url="https://ndownloader.figshare.com/files/39693688",
        checksum="md5:49f4237c802f067b9767c1de4b021999",
    ),
]

_BROKEN = (
    r"\[UpstreamUnavailableError\] Figshare article returned an unparseable 200 body "
    r"after 3 tries: UpstreamEnvelopeError\(['\"]no Figshare article in "
)


def _serving(routes: dict[str, tuple[int, object]], seen: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        status, body = routes[str(request.url)]
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doi",
    [
        "10.6084/m9.figshare.c.8708104.v1",
        "10.6084/m9.figshare.c.8708104",
        "10.25452/figshare.plus.c.8223469.v4",
        "10.25383/city.c.8323077",
        "10.6084/M9.FIGSHARE.C.8708104.V1",
    ],
)
async def test_a_collection_doi_names_no_article_and_asks_for_none(doi):
    # What /articles answered for the collection id, live: the old code raised this.
    gone = (404, {"message": "Entity not found: ArticleVersion", "code": "EntityNotFound"})
    routes = {
        f"{_API}/8708104/versions/1": gone,
        f"{_API}/8708104": gone,
        f"{_API}/8223469/versions/4": gone,
        f"{_API}/8323077": gone,
        f"{_API}/22306426": (200, _GRIFFITH),
    }
    seen: list[str] = []
    async with _serving(routes, seen) as c:
        assert await figshare.files(c, doi) == []
        assert seen == []
        # positive control: an article DOI on the same path is asked for and read
        assert await figshare.files(c, "10.57831/22306426") == _GRIFFITH_FILES
    assert seen == [f"GET {_API}/22306426"]


@pytest.mark.asyncio
async def test_a_portal_doi_with_the_id_after_the_slash_lists_its_files():
    seen: list[str] = []
    routes = {f"{_API}/22306426": (200, _GRIFFITH)}
    async with _serving(routes, seen) as c:
        # the first file's md5 is not computed yet: the supplied one is the checksum
        assert await figshare.files(c, "10.57831/22306426") == _GRIFFITH_FILES
    assert seen == [f"GET {_API}/22306426"]
    assert figshare._article_id("10.57831/22306426") == "22306426"
    assert figshare._article_id("10.57831/22306426.v2") == "22306426"
    assert figshare._article_id("10.57831/griffith") is None  # control: no digits, no id


@pytest.mark.asyncio
async def test_an_article_that_carries_no_doi_is_not_attached(caplog):
    """MMU 33549700 answers ``"doi": ""``: nothing shows it is the article a DOI that
    parses to its id names, so its files are not attached (the old code attached them)."""
    doi = "10.6084/m9.figshare.22306426"
    seen: list[str] = []
    async with _serving({f"{_API}/22306426": (200, dict(_GRIFFITH, doi=""))}, seen) as c:
        with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.figshare"):
            assert await figshare.files(c, doi) == []
    assert caplog.messages == [
        "figshare article 22306426 carries DOI '', not the requested "
        "'10.6084/m9.figshare.22306426'; no files attached"
    ]
    # control: the same article carrying the requested DOI is attached
    mine = dict(_GRIFFITH, doi=f"{doi}.v2")
    async with _serving({f"{_API}/22306426": (200, mine)}, seen) as c:
        assert await figshare.files(c, doi) == _GRIFFITH_FILES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # 34003890 (embargo_type "file") and 33834859 (confidential), live 2026-10-02
        {"doi": "10.6084/m9.figshare.34003890.v1", "files": None, "is_embargoed": True},
        {"doi": "10.82444/warw.33834859.v1", "files": None, "is_embargoed": True},
        # the same two and 31046215, live 2026-10-03: no "files" key at all
        {"doi": "10.6084/m9.figshare.34003890.v1", "is_embargoed": True},
        {"doi": "10.82444/warw.33834859.v1", "is_embargoed": True},
        {"doi": "10.6084/m9.figshare.31046215.v1", "is_embargoed": True},
        # 33820546, a metadata-only record, live
        {"doi": "10.82444/warw.33820546.v1", "files": [], "is_embargoed": False},
    ],
)
async def test_an_embargoed_or_metadata_only_article_lists_no_files(body):
    aid = body["doi"].split(".")[-2]
    seen: list[str] = []
    async with _serving({f"{_API}/{aid}/versions/1": (200, body)}, seen) as c:
        assert await figshare.files(c, body["doi"]) == []
    assert seen == [f"GET {_API}/{aid}/versions/1"]


def _file(**changes: object) -> dict:
    return {"doi": _GRIFFITH["doi"], "files": [{**_GRIFFITH["files"][1], **changes}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},  # was read as no files
        {"message": "Internal error"},
        {"doi": "10.57831/22306426"},  # no file list: was read as no files
        {"files": _GRIFFITH["files"]},  # no DOI: was attached unchecked
        {**_GRIFFITH, "doi": 22306426},
        {**_GRIFFITH, "doi": None},
        {**_GRIFFITH, "files": None},  # null, but not embargoed
        {**_GRIFFITH, "files": None, "is_embargoed": "true"},
        {**_GRIFFITH, "files": {"39679450": _GRIFFITH["files"][0]}},
        {**_GRIFFITH, "files": ["Karin_Vanessa_TIG.JPG"]},  # was a bare AttributeError
        {**_GRIFFITH, "files": [None]},
        _file(is_link_only=None),
        _file(is_link_only="false"),  # a non-empty string: was read as link-only, dropped
        _file(is_link_only=0),
        _file(name=None),  # was the file ""
        _file(name=7),
        _file(download_url=None),  # was the URL ""
        _file(download_url=["https://ndownloader.figshare.com/files/39693688"]),
        _file(size="2685661"),  # was coerced to an int by the model
        _file(size=True),
        _file(size=2685661.0),
        _file(computed_md5=7),  # was the checksum "md5:7"
        _file(computed_md5=["49f4237c802f067b9767c1de4b021999"]),
        _file(computed_md5="", supplied_md5=7),
    ],
)
async def test_a_malformed_article_is_upstream_trouble_not_no_files(body):
    seen: list[str] = []
    async with _serving({f"{_API}/22306426": (200, body)}, seen) as c:
        with pytest.raises(UpstreamUnavailableError, match=f"^{_BROKEN}"):
            await figshare.files(c, "10.57831/22306426")
    assert seen == [f"GET {_API}/22306426"] * 3  # retried, then raised
    # Positive control: the live answer, a link-only file with nulls, and absent or null
    # optional fields all read.
    link = {"is_link_only": True, "name": None, "download_url": None, "computed_md5": None}
    loose = _file(size=None, computed_md5=None, supplied_md5=None)
    loose["files"].insert(0, link)
    async with _serving({f"{_API}/22306426": (200, _GRIFFITH)}, seen) as c:
        assert await figshare.files(c, "10.57831/22306426") == _GRIFFITH_FILES
    async with _serving({f"{_API}/22306426": (200, loose)}, seen) as c:
        assert await figshare.files(c, "10.57831/22306426") == [
            FileEntry(
                name="Karin_Vanessa_TIG.JPG",
                size=None,
                url="https://ndownloader.figshare.com/files/39693688",
                checksum=None,
            )
        ]
    bare = copy.deepcopy(_file())
    for k in ("size", "computed_md5", "supplied_md5"):
        del bare["files"][0][k]
    async with _serving({f"{_API}/22306426": (200, bare)}, seen) as c:
        [entry] = await figshare.files(c, "10.57831/22306426")
    assert (entry.size, entry.checksum) == (None, None)


@live_only
@pytest.mark.asyncio
async def test_live_an_embargoed_article_resolves_without_files():
    async with httpx.AsyncClient(timeout=60) as client:
        record = await datacite.resolve(client, "10.6084/m9.figshare.31046215.v1")
        # control: Figshare still answers this article, embargoed, with no file list
        body = await _http.request_json(
            client, "GET", f"{_API}/31046215/versions/1", service="Figshare article", expect=dict
        )
        # control: an article with files still lists them through the same resolve
        listed = await datacite.resolve(client, "10.57831/22306426")
    assert (body["is_embargoed"], "files" in body) == (True, False)
    assert record.source == "figshare"
    assert record.files == []
    assert len(listed.files) >= 2


@live_only
@pytest.mark.asyncio
async def test_live_a_collection_doi_resolves_without_files():
    async with httpx.AsyncClient(timeout=60) as client:
        record = await datacite.resolve(client, "10.6084/m9.figshare.c.8708104.v1")
        # control: the collection id asked of /articles is not an article, live
        with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] Figshare article → HTTP 404"):
            await _http.request_json(
                client,
                "GET",
                f"{_API}/8708104/versions/1",
                service="Figshare article",
                expect=dict,
            )
    assert record.source == "figshare"
    assert record.files == []


@live_only
@pytest.mark.asyncio
async def test_live_a_griffith_doi_lists_its_files_with_md5s():
    async with httpx.AsyncClient(timeout=60) as client:
        record = await datacite.resolve(client, "10.57831/22306426")
    by_name = {f.name: f for f in record.files}
    assert record.source == "figshare"
    assert set(by_name) >= {"Dear Alice_TheImmersiveGuitar_Titles.mp4", "Karin_Vanessa_TIG.JPG"}
    assert by_name["Dear Alice_TheImmersiveGuitar_Titles.mp4"].checksum == (
        "md5:25c1ea2494abfe66c45478eb9f0e85e9"
    )
    assert by_name["Karin_Vanessa_TIG.JPG"].url == (
        "https://ndownloader.figshare.com/files/39693688"
    )
