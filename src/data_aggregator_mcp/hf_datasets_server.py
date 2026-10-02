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
DEFAULT_TIMEOUT = 30.0
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


def _name(p: dict) -> str:
    """The file's path on the dataset's conversion branch, ``<config>/<dir>/<file>``.
    The directory is the URL's, not ``split``: HF puts a split it converted only in
    part (its first 5 GB) under ``partial-<split>`` and a split of over 10,000 files
    under ``<split>-part<n>``, so a name built from ``split`` passed a partial file for
    the whole split and gave the parts' files one name each."""
    directory, filename = p["url"].rsplit("/", 2)[1:]
    return f"{p['config']}/{directory}/{filename}"


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
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        expect=dict,
        empty_answer=_is_no_view,
        no_content_returns=None,
    )
    if body is None:
        raise NotFoundError(f"{_SERVICE} has no readable converted view of {ds_id!r}")
    entries = body.get("parquet_files", [])
    files = [
        FileEntry(
            name=_name(p),
            url=p["url"],
            size=p.get("size"),
            source="hf-datasets-server",
        )
        for p in entries
        if p.get("url") and p.get("config") and p.get("split")
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
