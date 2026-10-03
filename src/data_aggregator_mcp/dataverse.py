"""Dataverse file-manifest resolver — the record's OWN Dataverse installation.

Native API: GET <base>/api/datasets/:persistentId/?persistentId=doi:<doi> →
data.latestVersion.files[] (an anonymous caller gets the latest RELEASED version; a
deaccessioned dataset answers with no ``latestVersion``). Dataverse also mints a DOI for
each file, which the dataset endpoint answers with 404; GET
<base>/api/files/:persistentId/ answers it with the one file. Download =
<base>/api/access/datafile/<id> (303→signed S3; the generic fetch engine follows
redirects), no auth for a public file. A restricted file, a file under an embargo that
has not ended, and a file past its retention period answer the download with 403, so
none of them is listed.

There are ~100 Dataverse installations (Harvard, DataverseNO, DANS, DaRUS, ...), and a
DOI is only resolvable on the one that holds it. The installation is taken from the
record's landing URL (DataCite ``attributes.url``) when known; otherwise
``DATAVERSE_BASE_URL`` if the operator set one; otherwise Harvard, but ONLY for
Harvard's own DOI prefix. Anything else has no knowable installation, so no listing is
attempted (logged) rather than querying a server that cannot hold the record.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

DEFAULT_BASE_URL = "https://dataverse.harvard.edu"
# Harvard Dataverse mints under 10.7910; only those DOIs may default to Harvard.
_HARVARD_DOI_PREFIX = "10.7910/"
_WEB_SCHEMES = ("http", "https")
_GET = "GET"
_DATASET_SERVICE = "Dataverse dataset"
_FILE_SERVICE = "Dataverse file"

# The optional fields `_entry` and `_downloadable` read, at the JSON type each is read
# as, on a file (a ``files[]`` element, or the file endpoint's ``data``) and on its
# ``dataFile``. ``label``, ``restricted`` and ``dataFile.id`` are required (`_is_file`).
_FILE_FIELDS = {"directoryLabel": str}
_DATAFILE_FIELDS = {
    "filesize": int,
    "originalFileName": str,
    "originalFileSize": int,
    "checksum": dict,
    "embargo": dict,
    "retention": dict,
}
_CHECKSUM_FIELDS = {"type": str, "value": str}

logger = logging.getLogger(__name__)


def _today() -> date:
    """The date embargo and retention dates are compared with. The server compares
    with its own local date, so on the boundary day a file may be listed or skipped
    some hours early or late."""
    return date.today()


def _base_url(doi: str, landing_url: str | None) -> str | None:
    if landing_url:
        try:
            parts = urlsplit(landing_url)
        except ValueError as exc:  # e.g. an unclosed IPv6 bracket in the record's URL
            logger.warning("ignoring malformed landing URL %r for %s: %s", landing_url, doi, exc)
        else:
            if parts.scheme in _WEB_SCHEMES and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
    env = os.environ.get("DATAVERSE_BASE_URL")
    if env:
        return env.rstrip("/")
    if doi.startswith(_HARVARD_DOI_PREFIX):  # digits, "." and "/": no case to fold
        return DEFAULT_BASE_URL
    return None


def _opt(value: object, kind: type) -> bool:
    """Absent or null, or a ``kind`` (``True`` is not an ``int``)."""
    return value is None or (type(value) is int if kind is int else isinstance(value, kind))


def _fields(item: object, kinds: Mapping[str, type]) -> bool:
    return isinstance(item, dict) and all(_opt(item.get(k), t) for k, t in kinds.items())


def _is_date(value: object) -> bool:
    """An ISO ``YYYY-MM-DD`` date, as Dataverse writes embargo and retention dates."""
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_file(f: object) -> bool:
    """A file as `_entry` and `_downloadable` read it: a str ``label``, a bool
    ``restricted``, a ``dataFile`` with an int ``id``, and every optional field at the
    type it is read as."""
    if not (
        isinstance(f, dict)
        and _fields(f, _FILE_FIELDS)
        and isinstance(f.get("label"), str)
        and type(f.get("restricted")) is bool
    ):
        return False
    df = f.get("dataFile")
    if not (isinstance(df, dict) and _fields(df, _DATAFILE_FIELDS) and type(df.get("id")) is int):
        return False
    embargo, retention = df.get("embargo"), df.get("retention")
    return (
        _fields(df.get("checksum") or {}, _CHECKSUM_FIELDS)
        and (embargo is None or _is_date(embargo.get("dateAvailable")))
        and (retention is None or _is_date(retention.get("dateUnavailable")))
    )


def _check_dataset(body: dict) -> None:
    data = body.get("data")
    version = data.get("latestVersion") if isinstance(data, dict) else None
    files = version.get("files") if isinstance(version, dict) else None
    if not (
        isinstance(data, dict)
        and type(data.get("id")) is int
        and _opt(version, dict)
        and (files is None or (isinstance(files, list) and all(_is_file(f) for f in files)))
    ):
        raise _http.UpstreamEnvelopeError(f"no Dataverse dataset in {body!r:.200}")


def _check_file(body: dict) -> None:
    if not _is_file(body.get("data")):
        raise _http.UpstreamEnvelopeError(f"no Dataverse file in {body!r:.200}")


def _downloadable(f: dict[str, Any]) -> bool:
    """False for a restricted file, a file under an embargo that has not ended, and a
    file past its retention period: Dataverse answers their download with 403
    (``FileUtil.isActivelyEmbargoed``: available after today; ``isRetentionExpired``:
    unavailable before today)."""
    df = f["dataFile"]
    today = _today()
    embargo, retention = df.get("embargo"), df.get("retention")
    return not (
        f["restricted"]
        or (embargo is not None and date.fromisoformat(embargo["dateAvailable"]) > today)
        or (retention is not None and date.fromisoformat(retention["dateUnavailable"]) < today)
    )


def _entry(base: str, f: dict[str, Any]) -> FileEntry:
    df = f["dataFile"]
    url = f"{base}/api/access/datafile/{df['id']}"
    if df.get("originalFileName"):
        # Ingested tabular file: Dataverse serves a derived .tab by default, but the
        # checksum it publishes is the ORIGINAL upload's. List the original so name,
        # size, url and checksum all describe the same bytes.
        name, size = df["originalFileName"], df.get("originalFileSize")
        url += "?format=original"
    else:
        name, size = f["label"], df.get("filesize")
    if f.get("directoryLabel"):
        name = f"{f['directoryLabel']}/{name}"
    checksum = df.get("checksum") or {}
    algo, value = checksum.get("type"), checksum.get("value")
    return FileEntry(
        name=name,
        size=size,
        url=url,
        # "MD5", "SHA-1", "SHA-256", "SHA-512" → hashlib's "md5", "sha1", ...
        checksum=f"{algo.lower().replace('-', '')}:{value}" if algo and value else None,
    )


async def files(
    client: httpx.AsyncClient, doi: str, *, landing_url: str | None = None
) -> list[FileEntry]:
    base = _base_url(doi, landing_url)
    if base is None:
        logger.warning(
            "Dataverse DOI %s: installation unknown (no landing URL, no DATAVERSE_BASE_URL, "
            "not a Harvard 10.7910 DOI); no file listing attempted",
            doi,
        )
        return []
    params = {"persistentId": f"doi:{doi}"}
    body = await _http.request_json(
        client,
        _GET,
        f"{base}/api/datasets/:persistentId/",
        service=_DATASET_SERVICE,
        params=params,
        expect=dict,
        check=_check_dataset,
        not_found_returns=None,
    )
    if body is not None:
        listed = (body["data"].get("latestVersion") or {}).get("files") or []
    else:
        # Not a dataset DOI. Dataverse also mints one per file (85 of 100 random
        # Harvard DOIs that DataCite types as Dataset, 2026-10-02).
        body = await _http.request_json(
            client,
            _GET,
            f"{base}/api/files/:persistentId/",
            service=_FILE_SERVICE,
            params=params,
            expect=dict,
            check=_check_file,
            not_found_returns=None,
        )
        if body is None:
            logger.warning(
                "Dataverse DOI %s: %s has no dataset or file with it; no file listing",
                doi,
                base,
            )
            return []
        listed = [body["data"]]
    return [_entry(base, f) for f in listed if _downloadable(f)]
