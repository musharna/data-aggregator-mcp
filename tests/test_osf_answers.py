"""Each OSF file-listing answer beside how the resolver must read it.

Probed live 2026-10-02 (api.osf.io/v2, anonymous):
- A listing page is ``200 {"data": [...], "links": {"first", "last", "prev", "next",
  "meta": {"total", "per_page"}}, "meta": {"version": "2.0"}}``; an empty folder or node
  has ``"data": []`` and ``"next": null``. Every next link and folder listing link is on
  ``https://api.osf.io/v2/``, including forced paging (``?page[size]=4`` → ``?page=2&
  page%5Bsize%5D=4``).
- Registrations (``StudyRegistration`` DOIs, 297,858 of cos.osf's 554,784) answer on the
  same ``/nodes/<guid>/files/osfstorage/`` path, their folders listed under
  ``/registrations/``; their files sit in an ``Archive of OSF Storage`` folder.
- 22 pages (78 entries, 8 nodes: projects, registrations, nested folders, a withdrawn
  preprint project) all pass ``_check_page``. Each file carried a non-empty name, an int
  size, a str md5 and a download link; each folder its listing link.
- Before the check, a 200 without ``data`` or ``links`` read as "no files" or ended the
  walk early, an entry with a wrong-typed field escaped as a bare ``AttributeError`` or
  pydantic error, a nameless file became the folder path itself, a file without a download
  link got ``url=""``, and a next or folder link to any host was requested.
"""

from __future__ import annotations

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, osf
from data_aggregator_mcp.errors import UpstreamUnavailableError


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(_http._ratelimit, "acquire", _ns)


_ROOT = "https://api.osf.io/v2/nodes/sv3qh/files/osfstorage/"
_DATAFILES = _ROOT + "68d5ca4f09181e9857432c0c/"

# Verbatim from the live osf.io/sv3qh root listing, trimmed to the fields the walk reads.
_FILE = {
    "id": "68d5cdadcfbda77f33b8f6eb",
    "type": "files",
    "attributes": {
        "name": "Metadata.docx",
        "kind": "file",
        "size": 23183,
        "extra": {"hashes": {"md5": "2eb01bb7a94117ac5053b0cdd03fc3c1"}, "downloads": 1},
    },
    "links": {"download": "https://osf.io/download/f23vk/"},
}
_FOLDER = {
    "id": "68d5ca4f09181e9857432c0c",
    "type": "files",
    "attributes": {
        "name": "Datafiles",
        "kind": "folder",
        "size": None,
        "extra": {"hashes": {"md5": None, "sha256": None}},
    },
    "relationships": {"files": {"links": {"related": {"href": _DATAFILES, "meta": {}}}}},
    "links": {},
}


def _page(*items: object, next_url: str | None = None) -> dict:
    return {
        "data": list(items),
        "links": {
            "first": None,
            "last": None,
            "prev": None,
            "next": next_url,
            "meta": {"total": len(items), "per_page": 10},
        },
        "meta": {"version": "2.0"},
    }


_EMPTY = _page()


def _serving(root: object) -> httpx.AsyncClient:
    """The root listing answers ``root``; any folder listing on the API is empty. A
    request anywhere else fails the test outright (it is not an upstream error)."""

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert url.startswith("https://api.osf.io/v2/"), f"requested off the API: {url}"
        return httpx.Response(200, json=root if url == _ROOT else _EMPTY)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


_BROKEN = r"^\[UpstreamUnavailableError\] OSF files returned an unparseable 200 body after 3 tries: UpstreamEnvelopeError\([\'\"]no OSF file listing in "


def _with_attrs(item: dict, **attrs: object) -> dict:
    return {**item, "attributes": {**item["attributes"], **attrs}}


@pytest.mark.parametrize(
    "body",
    [
        {},  # was read as no files
        {"data": None, "links": {"next": None}},  # was read as no files
        {"errors": [{"detail": "Internal error"}]},  # was read as no files
        {"data": [_FILE]},  # no links: the walk stopped, whatever pages followed
        {"data": [_FILE], "links": {}},  # no next: the same
        {"data": [_FILE], "links": None},
        {"data": "x", "links": {"next": None}},
        _page("x"),  # was a bare AttributeError
        _page(None),
        _page({"links": {"download": "https://osf.io/download/f23vk/"}}),  # no attributes
        _page(_with_attrs(_FILE, name=None)),  # was the file "" (the folder path itself)
        _page(_with_attrs(_FILE, name="")),
        _page(_with_attrs(_FILE, name=7)),  # was a bare TypeError
        _page(_with_attrs(_FILE, kind=None)),  # was skipped as "not a file"
        _page(_with_attrs(_FILE, kind="symlink")),
        _page(_with_attrs(_FILE, size="23183")),  # was a pydantic error
        _page(_with_attrs(_FILE, size=True)),
        _page(_with_attrs(_FILE, size=1.5)),
        _page(_with_attrs(_FILE, extra={"hashes": {"md5": 7}})),  # was checksum "md5:7"
        _page({**_FILE, "links": {}}),  # was url=""
        _page({**_FILE, "links": {"download": ""}}),
        _page({**_FILE, "links": {"download": 7}}),
        _page({**_FILE, "links": None}),
        _page({**_FOLDER, "relationships": {}}),
        _page({**_FOLDER, "relationships": {"files": {"links": {"related": {"href": ""}}}}}),
        _page({**_FOLDER, "relationships": {"files": {"links": {"related": {"href": 7}}}}}),
        # Off the API: was requested.
        _page(
            {
                **_FOLDER,
                "relationships": {
                    "files": {"links": {"related": {"href": "https://evil.example/v2/"}}}
                },
            }
        ),
        _page(
            {
                **_FOLDER,
                "relationships": {
                    "files": {"links": {"related": {"href": "https://api.osf.io/v2"}}}
                },
            }
        ),
        _page(
            {
                **_FOLDER,
                "relationships": {
                    "files": {"links": {"related": {"href": "https://api.osf.io/v2.evil.example/"}}}
                },
            }
        ),
        _page(_FILE, next_url="https://evil.example/v2/nodes/sv3qh/files/osfstorage/?page=2"),
        _page(_FILE, next_url="http://api.osf.io/v2/nodes/sv3qh/files/osfstorage/?page=2"),
        {**_page(_FILE), "links": {"next": 2}},
    ],
)
async def test_a_broken_listing_is_an_error_not_no_files(body):
    async with _serving(body) as c:
        with pytest.raises(UpstreamUnavailableError, match=_BROKEN):
            await osf.files(c, "10.17605/OSF.IO/SV3QH")
    # Positive controls: an empty listing is no files; a real page is read whole.
    async with _serving(_EMPTY) as c:
        assert await osf.files(c, "10.17605/OSF.IO/SV3QH") == []
    async with _serving(_page(_FILE, _FOLDER)) as c:
        (got,) = await osf.files(c, "10.17605/OSF.IO/SV3QH")
    assert got.model_dump(exclude_none=True) == {
        "name": "Metadata.docx",
        "size": 23183,
        "url": "https://osf.io/download/f23vk/",
        "checksum": "md5:2eb01bb7a94117ac5053b0cdd03fc3c1",
    }


async def test_a_file_without_a_size_or_md5_is_still_listed():
    """Null size and md5 are legitimate (a folder carries both null); the file is listed
    with neither, not refused and not given ``md5:None``."""
    bare = _with_attrs(_FILE, size=None, extra={"hashes": {"md5": None}})
    no_extra = {**_FILE, "attributes": {"name": "b.csv", "kind": "file"}}
    async with _serving(_page(bare, no_extra)) as c:
        got = await osf.files(c, "10.17605/OSF.IO/SV3QH")
    assert [(f.name, f.size, f.checksum) for f in got] == [
        ("Metadata.docx", None, None),
        ("b.csv", None, None),
    ]


# Every type a JSON value can take, one of each.
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(doc: dict, path: tuple, value: object) -> dict:
    rec = copy.deepcopy(doc)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


async def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Walks every field of a real page (a file and a folder) with every JSON type: each
    must be refused by the check or read cleanly, so a field the walk starts reading
    without the check fails here."""
    page = _page(_FILE, _FOLDER, next_url=None)
    osf._check_page(page)  # positive control: the real page passes and reads whole
    async with _serving(page) as c:
        assert [f.name for f in await osf.files(c, "10.17605/OSF.IO/SV3QH")] == ["Metadata.docx"]
    escaped = []
    for path in _paths(page):
        if not path:
            continue
        for value in _WRONG:
            body = _with(page, path, value)
            try:
                osf._check_page(body)
            except _http.UpstreamEnvelopeError:
                continue
            try:
                async with _serving(body) as c:
                    await osf.files(c, "10.17605/OSF.IO/SV3QH")
            except UpstreamUnavailableError:
                continue  # a loop or cap: named, not bare
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


def test_the_check_refuses_with_a_message_naming_osf():
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no OSF file listing in \{\}$"):
        osf._check_page({})
    osf._check_page(_EMPTY)  # positive control


@pytest.mark.parametrize(
    "doi",
    [
        "10.17605/osf.io/sv3qh?page=2",  # was sent as /nodes/sv3qh?page=2/files/...
        "10.17605/osf.io/sv3qh#x",
        "10.17605/osf.io/..",  # was sent as /v2/files/osfstorage/
        "10.17605/osf.io/sv3%2f",
        "10.17605/osf.io/sv 3qh",
        "10.17605/osf.io/",
        "",
    ],
)
async def test_a_doi_not_ending_in_a_guid_sends_nothing(doi):
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_page(_FILE))

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as c:
        assert await osf.files(c, doi) == []
        assert sent == []
        # Positive control: the same client lists a real guid's files.
        assert [f.name for f in await osf.files(c, "10.17605/OSF.IO/SV3QH")] == ["Metadata.docx"]
    assert [str(r.url) for r in sent] == [_ROOT]


def test_guid_is_the_lower_cased_last_segment():
    assert osf._guid("10.17605/OSF.IO/SV3QH") == "sv3qh"
    assert osf._guid("10.17605/c97pd") == "c97pd"  # 3 live cos.osf DOIs have no "osf.io/"
    assert osf._guid("10.17605/osf.io/sv3qh?x") is None


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_a_registration_lists_its_archived_files():
    """A registration DOI answers on /nodes/ and its folders under /registrations/; every
    page passes the check and every file keeps the archive folder in its path."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        got = await osf.files(c, "10.17605/osf.io/ufpc4")
    assert len(got) >= 9
    assert all(f.name.startswith("Archive of OSF Storage/") for f in got)
    assert all(f.checksum and f.checksum.startswith("md5:") for f in got)


@live_only
async def test_live_forced_paging_passes_the_check():
    """OSF's own next links (here forced at 4 per page) pass the on-API check, and the
    pages hold the folder's whole listing."""
    url: str | None = _DATAFILES + "?page%5Bsize%5D=4"
    seen = 0
    pages = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        while url:
            body = await _http.request_json(
                c, "GET", url, service="OSF files", expect=dict, check=osf._check_page
            )
            seen += len(body["data"])
            pages += 1
            url = body["links"]["next"]
    assert (pages, seen) == (3, 10)
