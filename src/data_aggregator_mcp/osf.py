"""OSF (osfstorage) file-manifest resolver — public nodes API, paginated, md5.

DOI 10.17605/OSF.IO/<guid> → node guid (lowercased). GET
https://api.osf.io/v2/nodes/<guid>/files/osfstorage/ → data[] (follow links.next to
paginate). A folder (kind=="folder") is listed through its
relationships.files.links.related.href, recursively, and its files keep their path
relative to the storage root ("Datafiles/raw/a.csv"): fetch plans basename
collisions on that path. Skipping folders once listed 1 of osf.io/sv3qh's 12 files.
Registrations answer on the same /nodes/ path (live 2026-10-02). download links
302→files.osf.io (engine follows redirects), no auth.

Every listing page is checked (``_check_page``): a 200 without its ``data`` list or
``links.next``, an entry missing a field the walk reads, or a next/folder link off
``API`` is a malformed answer (retried, then ``UpstreamUnavailableError``), never "no
files" or a request to another host.
"""

from __future__ import annotations

import re
from collections import deque

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

API = "https://api.osf.io/v2"
_GET = "GET"
# Runaway guard on listing requests per node, across every folder and page: ~20k
# files at OSF's 10 per page. Tripping it raises; a partial manifest is never returned.
_MAX_REQUESTS = 2000
# An OSF guid is lower-case alphanumeric; anything else must not reach the URL path.
_GUID = re.compile(r"[a-z0-9]+")


def _guid(doi: str) -> str | None:
    guid = doi.rpartition("/")[2].lower()
    return guid if _GUID.fullmatch(guid) else None


def _dig(value: object, *keys: str) -> object:
    """``value[k1][k2]…``, or None where a level is missing or not a dict."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _api_link(value: object) -> bool:
    return isinstance(value, str) and value.startswith(f"{API}/")


def _is_entry(item: object) -> bool:
    """A named file or folder carrying every field the walk reads, at the type it reads
    it as: a folder its listing link on ``API``; a file its download link, an int or
    null size and a str or null md5."""
    name, kind = _dig(item, "attributes", "name"), _dig(item, "attributes", "kind")
    if not (isinstance(name, str) and name):
        return False
    if kind == "folder":
        return _api_link(_dig(item, "relationships", "files", "links", "related", "href"))
    size, md5 = _dig(item, "attributes", "size"), _dig(item, "attributes", "extra", "hashes", "md5")
    download = _dig(item, "links", "download")
    return (
        kind == "file"
        and (size is None or type(size) is int)
        and isinstance(md5, str | None)
        and isinstance(download, str)
        and bool(download)
    )


def _check_page(body: dict) -> None:
    links, data = body.get("links"), body.get("data")
    nxt = _dig(links, "next")
    if not (
        isinstance(data, list)
        and all(_is_entry(item) for item in data)
        and isinstance(links, dict)
        and "next" in links
        and (nxt is None or _api_link(nxt))
    ):
        raise _http.UpstreamEnvelopeError(f"no OSF file listing in {body!r:.200}")


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    guid = _guid(doi)
    if not guid:
        return []
    out: list[FileEntry] = []
    # (first-page url, path prefix) per folder still to list; the root has prefix "".
    folders: deque[tuple[str, str]] = deque([(f"{API}/nodes/{guid}/files/osfstorage/", "")])
    requested: set[str] = set()
    while folders:
        url: str | None
        url, prefix = folders.popleft()
        while url:
            if url in requested:
                raise UpstreamUnavailableError(
                    f"OSF files for {guid}: listing revisited {url} (pagination or folder loop)"
                )
            if len(requested) >= _MAX_REQUESTS:
                raise UpstreamUnavailableError(
                    f"OSF files for {guid}: still unfinished after {_MAX_REQUESTS} listing "
                    "requests; refusing to return a partial manifest"
                )
            requested.add(url)
            body = await _http.request_json(
                client, _GET, url, service="OSF files", expect=dict, check=_check_page
            )
            for item in body["data"]:
                attrs = item["attributes"]
                name = f"{prefix}{attrs['name']}"
                if attrs["kind"] == "folder":
                    folders.append(
                        (item["relationships"]["files"]["links"]["related"]["href"], f"{name}/")
                    )
                    continue
                md5 = _dig(attrs, "extra", "hashes", "md5")
                out.append(
                    FileEntry(
                        name=name,
                        size=attrs.get("size"),
                        url=item["links"]["download"],
                        checksum=f"md5:{md5}" if md5 else None,
                    )
                )
            url = body["links"]["next"]
    return out
