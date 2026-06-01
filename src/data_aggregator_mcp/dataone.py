"""DataONE federation (eco/environmental) — search + resolve + verified fetch.

Discovery hits the Coordinating Node Solr index. Data bytes live on Member
Nodes, so ``resolve`` does a per-object ``/resolve/`` hop (Task 4) to get the
streamable MN url. Checksums vary per object (MD5 or SHA256); the prefix is
built from ``checksumAlgorithm`` so ``fetch.py``'s ``_hasher`` verifies either.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import Creator, DataResource, compact

SOLR = "https://cn.dataone.org/cn/v2/query/solr/"
PREFIXES = {"dataone"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

_SEARCH_FL = (
    "identifier,title,author,origin,formatId,dateUploaded,datePublished,dateModified,resourceMap"
)


def _year(*vals: str | None) -> int | None:
    for v in vals:
        if v and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    return None


def _creators(doc: dict) -> list[Creator]:
    origin = doc.get("origin")
    if isinstance(origin, list) and origin:
        return [Creator(name=str(o)) for o in origin if o]
    author = doc.get("author")
    return [Creator(name=str(author))] if author else []


def _normalize(doc: dict) -> DataResource:
    pid = doc.get("identifier", "")
    return DataResource(
        id=f"dataone:{pid}",
        source="dataone",
        kind="dataset",
        title=doc.get("title") or "",
        creators=_creators(doc),
        year=_year(doc.get("datePublished"), doc.get("dateUploaded")),
        last_updated=doc.get("dateModified"),
        files=[],
    )


async def _solr(
    client: httpx.AsyncClient, query: str, *, rows: int, fl: str, start: int = 0
) -> tuple[int, list[dict]]:
    body = await _http.request_json(
        client,
        "GET",
        SOLR,
        service="DataONE search",
        params={"q": query, "fl": fl, "rows": str(rows), "start": str(start), "wt": "json"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    resp = body.get("response", {}) or {}
    return int(resp.get("numFound", 0)), (resp.get("docs") or [])


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    q = f"({query}) AND formatType:METADATA"
    total, docs = await _solr(client, q, rows=capped, start=offset, fl=_SEARCH_FL)
    return total, [compact(_normalize(d)) for d in docs]
