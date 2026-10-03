"""Each Dryad API answer beside how the manifest must read it.

Probed live 2026-10-02 (datadryad.org/api/v2):
- ``/datasets/doi%3A10.5061%2Fdryad.x`` answers; the unencoded ``/datasets/doi:10.5061/
  dryad.x`` is a 404, and an upper-cased DOI answers the same record.
- A DOI Dryad does not hold is ``404 {"error":"not-found"}``. 95,914 of DataCite's
  170,511 Dryad DOIs are per-file DOIs of the old repository (``10.5061/dryad.50kt0/1``,
  resourceType ``DataFile``, ``IsPartOf`` the package), and every one of those is such a 404.
- A dataset Dryad will not show is a 200 of ``identifier``, ``id`` and a ``message``, with
  no links (dryad-app ``StashApi::Dataset#simple_identifier``).
- A file page always carries ``_links``, ``count``, an int ``total`` and
  ``_embedded.stash:files`` (``[]`` past the last page); ``next`` is a relative path that
  keeps ``per_page``, absent on the last page; ``per_page`` over 100 is served as 100.
- 103 dataset answers and 22 file pages (315 files): every link a path on datadryad.org,
  every file's self link ``/api/v2/files/<n>``, ``path``/``digest``/``digestType``
  strings (``sha-256`` or ``md5``), ``size`` an int.
"""

from __future__ import annotations

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, datacite, dryad
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_API = "https://datadryad.org/api/v2"
_PKG_DOI = "10.5061/dryad.50kt0"
_PKG_URL = f"{_API}/datasets/doi%3A10.5061%2Fdryad.50kt0"
_FILES_URL = f"{_API}/versions/19295/files?per_page=100"
# Trimmed from the live answers for 10.5061/dryad.50kt0 (curies and long fields dropped).
_PKG = {
    "_links": {
        "self": {"href": "/api/v2/datasets/doi%3A10.5061%2Fdryad.50kt0"},
        "stash:versions": {"href": "/api/v2/datasets/doi%3A10.5061%2Fdryad.50kt0/versions"},
        "stash:version": {"href": "/api/v2/versions/19295"},
        "stash:download": {"href": "/api/v2/datasets/doi%3A10.5061%2Fdryad.50kt0/download"},
    },
    "identifier": "doi:10.5061/dryad.50kt0",
    "id": 19295,
    "versionNumber": 1,
    "curationStatus": "Published",
    "visibility": "public",
}
_NYMPHS = {
    "_links": {
        "self": {"href": "/api/v2/files/65220"},
        "stash:dataset": {"href": "/api/v2/datasets/doi%3A10.5061%2Fdryad.50kt0"},
        "stash:version": {"href": "/api/v2/versions/19295"},
        "stash:files": {"href": "/api/v2/versions/19295/files"},
        "stash:download": {"href": "/api/v2/files/65220/download"},
    },
    "path": "nymphs_replica 1.raw.zip",
    "size": 2787924579,
    "mimeType": "application/zip",
    "status": "created",
    "digest": "22e43de1d5f0df7720a0949bdffa2f8b",
    "digestType": "md5",
}
# From the live answer for 10.5061/dryad.b5mkkwhrk.
_SHA_FILE = {
    "_links": {"self": {"href": "/api/v2/files/4356958"}},
    "path": "0.03125_2to1_Solution.raw",
    "size": 46680834,
    "mimeType": "image/RAW",
    "status": "copied",
    "digest": "aa7ba327021e3929ea9348a836893196ed29969ae48069fbc371d18e9b934f2d",
    "digestType": "sha-256",
}
_NYMPHS_ENTRY = FileEntry(
    name="nymphs_replica 1.raw.zip",
    size=2787924579,
    url="https://datadryad.org/downloads/file_stream/65220",
    checksum="md5:22e43de1d5f0df7720a0949bdffa2f8b",
)
# The live answer to a DOI Dryad does not hold, and to a dataset it will not show.
_NOT_HELD = httpx.Response(404, json={"error": "not-found"})
_NOT_SHOWN = {
    "identifier": "doi:10.5061/dryad.x",
    "id": 1,
    "message": "Identifier cannot be viewed. Either you lack permission to view it, "
    "or it is missing required elements.",
}


def _page(*files: dict, total: int | None = None, nxt: str | None = None) -> dict:
    links: dict = {"self": {"href": "/api/v2/versions/19295/files?per_page=100"}}
    if nxt is not None:
        links["next"] = {"href": nxt}
    return {
        "_links": links,
        "count": len(files),
        "total": len(files) if total is None else total,
        "_embedded": {"stash:files": list(files)},
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


_REQUEST_BUDGET = 20  # the longest planned walk here is 5 requests


def _dryad(routes: dict[str, object], seen: list[httpx.Request]) -> httpx.AsyncClient:
    """Answers each exact URL in ``routes`` (a body, or a whole response) and 418 to
    anything else, so a request the test did not plan fails loudly. Past
    ``_REQUEST_BUDGET`` requests it answers 418 too, so a walk that stops checking its
    guards fails instead of hanging."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) > _REQUEST_BUDGET:
            return httpx.Response(418, text="request budget spent")
        answer = routes.get(str(request.url))
        if answer is None:
            return httpx.Response(418, text=f"unplanned {request.url}")
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _files(routes: dict[str, object], doi: str = _PKG_DOI) -> list[FileEntry]:
    async with _dryad(routes, []) as client:
        return await dryad.files(client, doi)


# --- what each answer means -----------------------------------------------------------


async def test_a_doi_dryad_does_not_hold_has_no_files_rather_than_not_found():
    """A per-file DOI of Dryad's old repository is a DataCite record Dryad's API answers
    404 for; it was raised as NotFoundError, so resolving the DataCite record said "not
    found". Positive control: the package it is part of lists its files."""
    file_doi_url = f"{_API}/datasets/doi%3A10.5061%2Fdryad.50kt0%2F1"
    assert await _files({file_doi_url: _NOT_HELD}, "10.5061/dryad.50kt0/1") == []
    out = await _files({_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS)})
    assert out == [_NYMPHS_ENTRY]


async def test_a_dataset_dryad_will_not_show_has_no_files():
    """Dryad's answer for a dataset it will not show has no version link: no files, and
    no file request. Positive control: the shown dataset lists its file."""
    seen: list[httpx.Request] = []
    async with _dryad({_PKG_URL: _NOT_SHOWN}, seen) as client:
        assert await dryad.files(client, _PKG_DOI) == []
    assert [str(r.url) for r in seen] == [_PKG_URL]
    assert await _files({_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS)}) == [_NYMPHS_ENTRY]


# --- malformed answers ----------------------------------------------------------------


_BAD_DATASETS = [
    {},
    {"error": "internal"},
    {"message": 7},
    {"_links": {}},
    {"_links": [], "message": "x"},
    {"_links": {"stash:version": "/api/v2/versions/19295"}},
    {"_links": {"stash:version": {"href": 19295}}},
    {"_links": {"stash:version": {"href": "api/v2/versions/19295"}}},
    {"_links": {"stash:version": {"href": ".evil.example/api/v2/versions/1"}}},
    {"_links": {"stash:version": {"href": "@evil.example/api/v2/versions/1"}}},
    {"_links": {"stash:version": {"href": "https://evil.example/api/v2/versions/1"}}},
]


@pytest.mark.parametrize("body", _BAD_DATASETS)
async def test_a_malformed_dataset_answer_is_an_outage_not_an_empty_manifest(body):
    """A 200 without the version link read as "no files" (or, for a link that is not a
    path, sent the next request to another host). Positive control: the real answer."""
    seen: list[httpx.Request] = []
    async with _dryad({_PKG_URL: body}, seen) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] Dryad dataset returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(.no Dryad dataset in \{",
        ):
            await dryad.files(client, _PKG_DOI)
    assert [str(r.url) for r in seen] == [_PKG_URL] * 3
    assert await _files({_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS)}) == [_NYMPHS_ENTRY]


_OFF_HOST = [".evil.example/x", "@evil.example/x", "https://evil.example/x", "x", ""]
_BAD_PAGES = [
    {},
    {k: v for k, v in _page(_NYMPHS).items() if k != "total"},
    {**_page(_NYMPHS), "total": "1"},
    {**_page(_NYMPHS), "total": True},
    {**_page(_NYMPHS), "total": None},
    {k: v for k, v in _page(_NYMPHS).items() if k != "_embedded"},
    {**_page(_NYMPHS), "_embedded": [_NYMPHS]},
    {**_page(_NYMPHS), "_embedded": {"stash:files": {"0": _NYMPHS}}},
    {**_page(_NYMPHS), "_embedded": {"stash:files": ["nymphs_replica 1.raw.zip"]}},
    {k: v for k, v in _page(_NYMPHS).items() if k != "_links"},
    {**_page(_NYMPHS), "_links": {"next": "/api/v2/versions/19295/files?page=2"}},
    *[_page(_NYMPHS, total=2, nxt=href) for href in _OFF_HOST],
    *[{**_page(_NYMPHS), "_links": {"next": {"href": h}}} for h in (None, 2, ["/x"])],
]


@pytest.mark.parametrize("body", _BAD_PAGES)
async def test_a_malformed_file_page_is_an_outage_not_a_short_manifest(body):
    """A page without its file list or int total read as zero files or skipped the
    truncation check, and a next link that is not a path on Dryad's host was requested
    as ``https://datadryad.org`` + link (``.evil.example/x`` is the host
    ``datadryad.org.evil.example``). Positive control: the real page."""
    seen: list[httpx.Request] = []
    async with _dryad({_PKG_URL: _PKG, _FILES_URL: body}, seen) as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] Dryad files returned an unparseable 200 body after 3 tries: "
            r"UpstreamEnvelopeError\(.no Dryad file list in \{",
        ):
            await dryad.files(client, _PKG_DOI)
    assert [str(r.url) for r in seen] == [_PKG_URL] + [_FILES_URL] * 3
    assert await _files({_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS)}) == [_NYMPHS_ENTRY]


_WRONG = ["x", "/api/v2/x", 7, True, 1.5, [1], {"k": 1}, None]
# The file fields the manifest reads, and the values of each it accepts.
_READ = {
    ("path",): (str, type(None)),
    ("size",): (int, type(None)),
    ("digest",): (str, type(None)),
    ("digestType",): (str, type(None)),
    ("_links",): (dict, type(None)),
    ("_links", "self"): (),
    ("_links", "self", "href"): (),
}


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))


def _with(path, value):
    f = copy.deepcopy(_NYMPHS)
    *parents, last = path
    node = f
    for p in parents:
        node = node[p]
    node[last] = value
    return f


def test_no_wrong_typed_file_field_escapes_or_is_read_as_something_else():
    """Every field of a real file at every JSON type: a field the manifest reads must be
    refused unless the value has the type it is read as, and no other field may make the
    reader raise. A ``size`` of True or a ``digest`` of 7 would otherwise be listed as
    size 1 or checksum ``md5:7``."""
    page = _page(_NYMPHS)
    dryad._check_files(page)  # positive control: the real file passes and reads whole
    assert dryad._entry(_NYMPHS) == _NYMPHS_ENTRY
    wrong = []
    for path in _paths(_NYMPHS):
        if not path:
            continue
        for value in _WRONG:
            f = _with(path, value)
            accepted = path not in _READ or type(value) in _READ[path]
            try:
                dryad._check_files(_page(f))
            except _http.UpstreamEnvelopeError:
                if accepted and path in _READ:
                    wrong.append((path, value, "refused"))
                continue
            if not accepted:
                wrong.append((path, value, "accepted"))
                continue
            try:
                dryad._entry(f)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                wrong.append((path, value, type(exc).__name__))
    # A self link must name a file: a path that is not /api/v2/files/<n> is refused.
    for href in ("/api/v2/files/65220/download", "/api/v2/files/x", "/api/v2/files/"):
        with pytest.raises(_http.UpstreamEnvelopeError):
            dryad._check_files(_page(_with(("_links", "self", "href"), href)))
    assert wrong == []


# --- live -----------------------------------------------------------------------------


@live_only
async def test_live_a_per_file_doi_resolves_through_datacite_with_its_package_link():
    """10.5061/dryad.50kt0/1 is a DataCite record (resourceType DataFile) that Dryad's API
    answers 404 for: it raised NotFoundError out of resolve. Positive control: its
    package resolves with its seven files."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        rec = await datacite.resolve(client, "datacite:10.5061/dryad.50kt0/1")
        pkg = await datacite.resolve(client, "datacite:10.5061/dryad.50kt0")
    assert rec.source == "dryad" and rec.files == []
    assert any(link.target_id == "10.5061/dryad.50kt0" for link in rec.links), rec.links
    assert pkg.source == "dryad"
    assert [f.name for f in pkg.files][:1] == ["nymphs_replica 1.raw.zip"]
    assert len(pkg.files) == 7 and all(f.checksum.startswith("md5:") for f in pkg.files)
