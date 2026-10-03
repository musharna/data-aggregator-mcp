"""Figshare file-manifest resolver — public articles API, md5-checksummed, no auth.

The article id is the numeric run in a Figshare DOI: ``10.6084/m9.figshare.<id>[.vN]``
on figshare.com, ``<prefix>/<portal>.<id>[.vN]`` or ``<prefix>/<id>`` on institutional
portals (e.g. ``10.25405/ncl.33951526.v1``, Griffith's ``10.57831/22306426``), which
share the one global article id space. A collection DOI (``….c.<id>[.vN]``) carries a
collection id, not an article id, and lists no files of its own.
``download_url`` is an ndownloader URL that 302-redirects to signed S3 (the generic
fetch engine follows redirects).
"""

from __future__ import annotations

import logging
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

BASE_URL = "https://api.figshare.com/v2"
# httpx upper-cases the method, so "get" would send the same request.
_GET = "GET"
_ARTICLE_ID = re.compile(r"figshare\.(\d+)")
# Institutional portals: the id is the final all-digit segment, after a dot or the prefix
# slash, before an optional ``.vN`` version suffix. Only reached for DOIs DataCite
# attributes to a figshare client.
_TRAILING_ID = re.compile(r"[./](\d+)(?:\.v\d+)?$", re.IGNORECASE)
# ``10.6084/m9.figshare.c.8708104.v1``, ``10.25383/city.c.8323077``: a collection id (its
# own id space), which /articles answers with 404 or with some unrelated article.
_COLLECTION = re.compile(r"\.c\.\d+(?:\.v\d+)?$", re.IGNORECASE)
_VERSION = re.compile(r"\.v(\d+)$", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _article_id(doi: str) -> str | None:
    if _COLLECTION.search(doi):
        return None
    m = _ARTICLE_ID.search(doi) or _TRAILING_ID.search(doi)
    return m.group(1) if m else None


def _doi_key(doi: str, versioned: bool) -> str:
    """``doi`` as compared: case-insensitive, without its ``.vN`` unless versioned."""
    key = doi.strip().casefold()
    return key if versioned else _VERSION.sub("", key)


def _version(doi: str) -> str | None:
    m = _VERSION.search(doi.strip())
    return m.group(1) if m else None


def _is_file(f: object) -> bool:
    """A link-only file (hosted elsewhere, skipped), or every field ``files`` reads at
    the type it reads it as. A checksum Figshare has not computed yet is ``""``."""
    if not (isinstance(f, dict) and type(f.get("is_link_only")) is bool):
        return False
    return f["is_link_only"] or (
        isinstance(f.get("name"), str)
        and isinstance(f.get("download_url"), str)
        and (f.get("size") is None or type(f.get("size")) is int)
        and isinstance(f.get("computed_md5"), str | None)
        and isinstance(f.get("supplied_md5"), str | None)
    )


def _check_article(body: dict) -> None:
    """A DOI string and a file list. The list is null while the files are embargoed
    (which is also how a confidential article answers)."""
    files = body.get("files")
    listed = isinstance(files, list) and all(_is_file(f) for f in files)
    embargoed = files is None and body.get("is_embargoed") is True
    if not (isinstance(body.get("doi"), str) and (listed or embargoed)):
        raise _http.UpstreamEnvelopeError(f"no Figshare article in {body!r:.200}")


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    aid = _article_id(doi)
    if not aid:
        return []
    # A ``.vN`` DOI names one immutable version; /articles/{id} is whatever is current.
    # Versioned → that version's endpoint and an exact DOI match; unversioned → current.
    version = _version(doi)
    url = f"{BASE_URL}/articles/{aid}" + (f"/versions/{version}" if version else "")
    data = await _http.request_json(
        client, _GET, url, service="Figshare article", expect=dict, check=_check_article
    )
    got = data["doi"]
    if _doi_key(got, bool(version)) != _doi_key(doi, bool(version)):
        # The id was parsed from the DOI's shape; the article it names is someone else's
        # (or carries no DOI, so cannot be shown to be ours). Attaching its files would
        # be wrong data, so attach none (logged).
        logger.warning(
            "figshare article %s carries DOI %r, not the requested %r; no files attached",
            aid,
            got,
            doi,
        )
        return []
    out: list[FileEntry] = []
    for f in data["files"] or []:  # null while embargoed
        if f["is_link_only"]:
            continue
        md5 = f.get("computed_md5") or f.get("supplied_md5")
        out.append(
            FileEntry(
                name=f["name"],
                size=f.get("size"),
                url=f["download_url"],
                checksum=f"md5:{md5}" if md5 else None,
            )
        )
    return out
