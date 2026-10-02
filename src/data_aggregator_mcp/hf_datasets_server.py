# src/data_aggregator_mcp/hf_datasets_server.py
"""HuggingFace datasets-server: surface a dataset's auto-converted Parquet files
as FileEntries so the existing operate engines can query any HF dataset."""

from __future__ import annotations

import logging

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry

logger = logging.getLogger(__name__)

DSS_API = "https://datasets-server.huggingface.co"
MAX_DSS_FILES = 100
MAX_RETRIES = 2
_SERVICE = "HF datasets-server"
# httpx upper-cases the method and reads header names case-insensitively, so a
# spelling mutant of either sends the same request.
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
# Error statuses that answer "no converted view an anonymous caller can read", not an
# outage (the API's OpenAPI spec; probed 2026-10-02): 401 = gated, private or missing;
# 501 = a script dataset, a blocked one, a failed job, or a file list over 10 MB.
_NO_VIEW_STATUSES = (401, 501)


def _is_no_view(resp: httpx.Response) -> bool:
    return resp.status_code in _NO_VIEW_STATUSES


# Every converted file sits on the dataset's conversion branch, at
# ``https://huggingface.co/datasets/<id>/resolve/refs%2Fconvert%2Fparquet/<config>/<dir>/<file>``
# (37,280 of 37,280 files in 20 live answers, 2026-10-02).
_ON_BRANCH = "/resolve/refs%2Fconvert%2Fparquet/"


def _branch_path(url: str) -> str:
    """The file's path on the conversion branch, ``<config>/<dir>/<file>``; "" when
    the URL is not on it. The directory is not always ``split``: HF puts a split it
    converted only in part (its first 5 GB) under ``partial-<split>`` and a split of
    over 10,000 files under ``<split>-part<n>``, so a name built from ``split`` passed a
    partial file for the whole split and gave the parts' files one name each."""
    return url.partition(_ON_BRANCH)[2]


def _is_file(p: object) -> bool:
    """A converted file carrying every field the mapping reads, at its type: a URL
    with a ``<config>/<dir>/<file>`` path on the branch, and an int size or none."""
    if not (isinstance(p, dict) and isinstance(p.get("url"), str)):
        return False
    parts = _branch_path(p["url"]).split("/")
    return len(parts) == 3 and all(parts) and (p.get("size") is None or type(p["size"]) is int)


def _check_parquet(body: dict) -> None:
    files = body.get("parquet_files")
    if not (isinstance(files, list) and all(_is_file(p) for p in files)):
        raise _http.UpstreamEnvelopeError(f"no datasets-server parquet list in {body!r:.200}")


async def parquet_files(client: httpx.AsyncClient, ds_id: str) -> list[FileEntry]:
    """The datasets-server auto-converted Parquet files for ``ds_id``.

    Raises ``NotFoundError`` when the dataset has no converted view an anonymous
    caller can read (404; 401 gated or private; 501 unsupported) — the caller treats
    that as the normal "not operable via datasets-server" signal and keeps the raw
    siblings.
    """
    body = await _http.request_json(
        client,
        _GET,
        f"{DSS_API}/parquet",
        service=_SERVICE,
        params={"dataset": ds_id},
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        expect=dict,
        check=_check_parquet,
        empty_answer=_is_no_view,
        no_content_returns=None,
    )
    if body is None:
        raise NotFoundError(f"{_SERVICE} has no readable converted view of {ds_id!r}")
    files = [
        FileEntry(
            name=_branch_path(p["url"]),
            url=p["url"],
            size=p.get("size"),
            source="hf-datasets-server",
        )
        for p in body["parquet_files"]
    ]
    if len(files) > MAX_DSS_FILES:
        logger.warning(
            "datasets-server: %s exposes %d parquet files; capping to %d",
            ds_id,
            len(files),
            MAX_DSS_FILES,
        )
        files = files[:MAX_DSS_FILES]
    return files
