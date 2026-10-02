"""MetaboLights (metabolomics) file-manifest backend for OmicsDI-routed fetch.

Filenames come from the public EBI FTP directory listing itself, NOT the
MetaboLights WS ``/files`` API: the WS API returns *logical* names that don't
always match the physical file on the FTP mirror (e.g. the assay file is listed
as ``a_MTBLS1_NMR_metabolite_profiling…`` but stored as
``a_MTBLS1_metabolite_profiling…``), so WS-derived urls 404. The listing is the
same mirror we download from, so its names are guaranteed to resolve.

Top-level files only (raw data lives under the ``FILES/`` subdir, not descended).
Each carries the sha256 the study publishes in ``HASHES/metadata_sha256.json``; a
study without that file, or a file it omits, stays unverified. No size.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote, unquote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

FTP_PATH = "/pub/databases/metabolights/studies/public/{acc}"
FTP_DIR = "https://ftp.ebi.ac.uk" + FTP_PATH + "/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

# Every public study directory (3,497 of 3,497 on the mirror, 2026-10-02) and every
# OmicsDI MetaboLights accession sampled is `MTBLS<digits>`. The accession goes into
# the URL path, so nothing else may reach it.
_ACC = re.compile(r"MTBLS[0-9]+")

# Apache autoindex hrefs. First char excludes sort links (`?C=…`) and the
# absolute parent link (`/…`); the bounded `[^"]*` is linear (no backtracking).
_HREF = re.compile(r'href="([^"?/][^"]*)"')

# mod_autoindex writes each entry's href as html_escape(os_escape_path(name)), with a
# `./` before a name whose first segment holds a `:`. Once the HTML escaping is undone,
# an entry is one percent-escaped path segment: no raw `/` but a directory's trailing
# one, no raw `?` or `#`, never `.` or `..`.
_ENTRY = re.compile(r"(?:\./)?([^/?#]+)(/?)")


def _listing_files(page: str, acc: str) -> list[str]:
    """File names from the Apache autoindex page of study ``acc``, unescaped, in
    listing order; subdirectories (trailing ``/``) skipped. A page that is not that
    directory's index, or an entry that is not one name in it, is upstream trouble:
    reading it as a list of files would name files the study does not have, and
    reading it as no files would hide the study's."""
    if f"<title>Index of {FTP_PATH.format(acc=acc)}</title>" not in page:
        raise UpstreamUnavailableError(
            f"MetaboLights files: no directory index of {acc} in the answer: {page[:200]!r}"
        )
    out: list[str] = []
    for href in _HREF.findall(page):
        entry = _ENTRY.fullmatch(html.unescape(href))
        if entry is None or unquote(entry[1]) in (".", ".."):
            raise UpstreamUnavailableError(
                f"MetaboLights files: {href!r} in the index of {acc} is not a name in it"
            )
        if not entry[2]:
            out.append(unquote(entry[1]))
    return out


async def _published_sha256(client: httpx.AsyncClient, base: str) -> dict[str, str]:
    """``{filename: sha256}`` from the study's ``HASHES/metadata_sha256.json``. A 404 is
    a study that publishes none (its files stay unverified); any other failure raises —
    the hashes sit on the same mirror as the files, so hiding it would only defer it."""
    body = await _http.request_json(
        client,
        "GET",
        base + "HASHES/metadata_sha256.json",
        service="MetaboLights hashes",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={},
        expect=dict,
    )
    return {str(k): str(v) for k, v in body.items() if isinstance(v, str) and v}


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    if not _ACC.fullmatch(accession):
        raise NotFoundError(f"not a MetaboLights study accession: {accession!r}")
    base = FTP_DIR.format(acc=accession)
    resp = await _http.request_with_retry(
        client,
        "GET",
        base,
        service="MetaboLights files",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    names = _listing_files(resp.text, accession)
    sha256 = await _published_sha256(client, base)
    return [
        FileEntry(
            name=name,
            url=base + quote(name, safe=""),
            size=None,
            checksum=f"sha256:{sha256[name]}" if name in sha256 else None,
            source="metabolights",
        )
        for name in names
    ]
