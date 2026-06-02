# src/data_aggregator_mcp/hf_datasets_server.py
"""HuggingFace datasets-server: surface a dataset's auto-converted Parquet files
as FileEntries so the existing operate engines can query any HF dataset."""

from __future__ import annotations

import logging

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

logger = logging.getLogger(__name__)

DSS_API = "https://datasets-server.huggingface.co"
MAX_DSS_FILES = 100
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


async def parquet_files(client: httpx.AsyncClient, ds_id: str) -> list[FileEntry]:
    """The datasets-server auto-converted Parquet files for ``ds_id``.

    Raises ``NotFoundError`` (via ``_http.request_json``) when the dataset has no
    converted view — the caller treats that as the normal "not operable via
    datasets-server" signal and keeps the raw siblings.
    """
    body = await _http.request_json(
        client,
        "GET",
        f"{DSS_API}/parquet",
        service="HF datasets-server",
        params={"dataset": ds_id},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    entries = body.get("parquet_files", []) if isinstance(body, dict) else []
    files = [
        FileEntry(
            name=f"{p['config']}/{p['split']}/{p['url'].rsplit('/', 1)[-1]}",
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
