"""OpenNeuro file-manifest resolver — BIDS neuroimaging datasets.

OpenNeuro discovery rides the DataCite firehose (its 10.18112/openneuro.* DOIs,
client `sul.openneuro`, are indexed there), so there is no native search adapter.
This module is the bespoke fetch backend: datacite.resolve() dispatches an
OpenNeuro-sourced DOI here to populate files[]. The dataset id + version are
parsed from the DOI (10.18112/openneuro.<dsID>.v<tag>) and the snapshot's
top-level file manifest is fetched via GraphQL. Nested directories (directory:true)
are skipped in v1 — a documented follow-up. Files are unverified (no checksum).
"""

from __future__ import annotations

import json
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

GRAPHQL = "https://openneuro.org/crn/graphql"
# 10.18112/openneuro.ds000001.v1.0.0 → ("ds000001", "1.0.0")
_DOI_RE = re.compile(r"openneuro\.(ds\d+)\.v([\w.]+)", re.IGNORECASE)
_QUERY = '{{snapshot(datasetId:"{ds}",tag:"{tag}"){{files{{filename size directory urls}}}}}}'
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _parse(doi: str) -> tuple[str, str] | None:
    m = _DOI_RE.search(doi)
    return (m.group(1), m.group(2)) if m else None


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    parsed = _parse(doi)
    if parsed is None:
        return []
    ds, tag = parsed
    body = await _http.request_json(
        client,
        "POST",
        GRAPHQL,
        service="OpenNeuro snapshot files",
        content=json.dumps({"query": _QUERY.format(ds=ds, tag=tag)}),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        # No not_found_returns: this GraphQL endpoint answers 200 with
        # data.snapshot=null for a missing snapshot (handled below); a real
        # HTTP 404 means the endpoint itself moved and should fail loud.
    )
    snapshot = ((body or {}).get("data") or {}).get("snapshot") or {}
    out: list[FileEntry] = []
    for f in snapshot.get("files") or []:
        if f.get("directory"):
            continue
        urls = f.get("urls") or []
        if not urls:
            continue
        out.append(
            FileEntry(
                name=f.get("filename") or "",
                size=f.get("size"),
                url=urls[0],
                source="openneuro",
            )
        )
    return out
