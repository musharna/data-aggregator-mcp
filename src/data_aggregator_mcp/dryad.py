"""Dryad file-manifest resolver — MANIFEST-ONLY.

Dryad downloads are bearer-token-gated (API) and bot-challenge-protected (web), so the
generic fetch engine cannot stream them; Dryad is intentionally excluded from the fetch
allowlist (see server._DATACITE_FETCHABLE). This resolver still populates files[] for
discovery: names, sizes, and sha-256 checksums. Two-step: dataset → latest version →
files, the latter paged via _links.next to the end.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://datadryad.org/api/v2"
_HOST = "https://datadryad.org"
_PAGE_SIZE = 100  # Dryad's per_page ceiling; its default is 20
# Runaway guard: ~100k files at 100 per page. Tripping it raises; a partial manifest
# is never returned.
_MAX_PAGES = 1000
# Dryad's dataset path takes the whole "doi:<doi>" percent-encoded, "/" included:
# /datasets/doi%3A10.5061%2Fdryad.x answers, /datasets/doi:10.5061/dryad.x is a 404.
_ENCODE_ALL = ""
_GET = "GET"  # httpx upper-cases the method, so a lower-cased mutant is the same request
# A file's API link is this prefix and the file's number, which is also the id of the
# file's download stream.
_FILE_PATH = "/api/v2/files/"
_FILE_HREF = re.compile(re.escape(_FILE_PATH) + r"\d+")
_NOT_A_LINK = object()


def _is_path(href: object) -> bool:
    """A link Dryad's API serves on its own host: a path, never a URL or a host suffix.
    Every href is requested as ``_HOST + href``, so ``.evil.org/x`` or ``@evil.org/x``
    would name another host."""
    return isinstance(href, str) and href.startswith("/")


def _link(links: object, rel: str) -> object:
    """``links[rel]["href"]``, or None when the rel is absent. A rel that is not an object
    with an href gives ``_NOT_A_LINK``, which every caller's type check refuses."""
    if not isinstance(links, dict) or rel not in links:
        return None
    link = links[rel]
    if isinstance(link, dict) and link.get("href") is not None:
        return link["href"]
    return _NOT_A_LINK


def _check_dataset(body: dict) -> None:
    """A dataset with its latest version's link, or Dryad's answer for a dataset it will
    not show (``identifier``, ``id`` and a ``message``, no links: dryad-app
    ``StashApi::Dataset#simple_identifier``)."""
    if "_links" not in body and isinstance(body.get("message"), str):
        return
    if not _is_path(_link(body.get("_links"), "stash:version")):
        raise _http.UpstreamEnvelopeError(f"no Dryad dataset in {body!r:.200}")


def _opt(value: object, kind: type) -> bool:
    return value is None or type(value) is kind


def _is_file(f: object) -> bool:
    """Every field the manifest reads, at the type it reads it as (absent or null is
    fine), and a self link that names a file."""
    if not isinstance(f, dict):
        return False
    href = _link(f.get("_links"), "self")
    return (
        _opt(f.get("_links"), dict)
        and all(_opt(f.get(k), str) for k in ("path", "digest", "digestType"))
        and _opt(f.get("size"), int)
        and (href is None or (isinstance(href, str) and _FILE_HREF.fullmatch(href) is not None))
    )


def _check_files(body: dict) -> None:
    """One page of a version's file list: the files, the int total, and a next link
    (when there is one) on Dryad's host. Dryad always sends all three
    (dryad-app ``FilesController#files_output``), an empty list past the last page."""
    embedded = body.get("_embedded")
    nxt = _link(body.get("_links"), "next")
    if not (
        isinstance(embedded, dict)
        and isinstance(embedded.get("stash:files"), list)
        and all(_is_file(f) for f in embedded["stash:files"])
        and type(body.get("total")) is int
        and isinstance(body.get("_links"), dict)
        and (nxt is None or _is_path(nxt))
    ):
        raise _http.UpstreamEnvelopeError(f"no Dryad file list in {body!r:.200}")


def _entry(f: dict) -> FileEntry:
    """``f`` has passed ``_is_file``: a self link, when present, is ``_FILE_PATH<n>``."""
    digest, algo = f.get("digest"), f.get("digestType")
    href = _link(f.get("_links"), "self")
    return FileEntry(
        name=f.get("path") or "",
        size=f.get("size"),
        url=(
            f"{_HOST}/downloads/file_stream/{href.removeprefix(_FILE_PATH)}"
            if isinstance(href, str)
            else ""
        ),
        # "sha-256" -> "sha256", a hashlib name
        checksum=f"{algo.replace('-', '')}:{digest}" if (digest and algo) else None,
    )


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    """The files of the latest version Dryad shows of ``doi``. Empty when Dryad holds no
    dataset under the DOI (404) or will not show it: DataCite lists 95,914 per-file DOIs
    of Dryad's old repository (``10.5061/dryad.x/1``, resourceType ``DataFile``) that the
    v2 API does not know, and those records resolve with their DataCite metadata and a
    link to the package."""
    enc = quote(f"doi:{doi}", safe=_ENCODE_ALL)
    ds = await _http.request_json(
        client,
        _GET,
        f"{BASE_URL}/datasets/{enc}",
        service="Dryad dataset",
        expect=dict,
        not_found_returns={},  # no links, like the answer for a dataset Dryad will not show
        check=_check_dataset,
    )
    if "_links" not in ds:
        return []
    # The files listing is paginated (20 per page by default); reading only the first
    # page listed 20 of b5mkkwhrk's 31 files. Follow _links.next to the end.
    href: object = f"{_link(ds['_links'], 'stash:version')}/files?per_page={_PAGE_SIZE}"
    requested: set[object] = set()
    total: int
    embedded: list[dict] = []
    while isinstance(href, str):  # _check_files: a next link is a path, or there is none
        if href in requested:
            raise UpstreamUnavailableError(f"Dryad files for {doi}: pagination revisited {href}")
        if len(requested) >= _MAX_PAGES:
            raise UpstreamUnavailableError(
                f"Dryad files for {doi}: still unfinished after {_MAX_PAGES} pages; "
                "refusing to return a partial manifest"
            )
        requested.add(href)
        fr = await _http.request_json(
            client,
            _GET,
            f"{_HOST}{href}",
            service="Dryad files",
            expect=dict,
            check=_check_files,
        )
        embedded.extend(fr["_embedded"]["stash:files"])
        total = fr["total"]
        href = _link(fr["_links"], "next")
    if len(embedded) != total:
        raise UpstreamUnavailableError(
            f"Dryad files for {doi}: pagination ended with {len(embedded)} of {total} files"
        )
    return [_entry(f) for f in embedded]
