"""DataONE federation (eco/environmental) — search + resolve + verified fetch.

Discovery hits the Coordinating Node Solr index. Data bytes live on Member
Nodes, so ``resolve`` does a per-object ``/resolve/`` hop (Task 4) to get the
streamable MN url. Checksums vary per object (MD5 or SHA256); the prefix is
built from ``checksumAlgorithm`` so ``fetch.py``'s ``_hasher`` verifies either.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, compact

SOLR = "https://cn.dataone.org/cn/v2/query/solr/"
RESOLVE = "https://cn.dataone.org/cn/v2/resolve/{pid}"
PREFIXES = {"dataone"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

_SEARCH_FL = (
    "identifier,title,author,origin,formatId,dateUploaded,datePublished,dateModified,resourceMap"
)
_RESOLVE_FL = "identifier,title,author,origin,dateUploaded,datePublished,dateModified,resourceMap"
_DATA_FL = "identifier,fileName,size,checksum,checksumAlgorithm"


def _year(*vals: str | None) -> int | None:
    for v in vals:
        if v and isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
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
    doi = pid[4:] if pid[:4].lower() == "doi:" else None
    return DataResource(
        id=f"dataone:{pid}",
        source="dataone",
        kind="dataset",
        title=doc.get("title") or "",
        creators=_creators(doc),
        year=_year(doc.get("datePublished"), doc.get("dateUploaded")),
        last_updated=doc.get("dateModified"),
        doi=doi,
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


def _first_url(xml_text: str) -> str | None:
    """First <url> in a DataONE ObjectLocationList (namespace-agnostic), or None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "url" and el.text:
            return el.text.strip()
    return None


async def _object_url(client: httpx.AsyncClient, pid: str) -> str | None:
    """Resolve a data PID to a Member-Node byte url. CN ``/cn/v2/resolve/`` answers
    303 with the MN url in the ``Location`` header (its body also carries the
    ObjectLocationList). We must NOT follow the redirect — the client follows by
    default and would download the object *bytes* instead of the locator, then fail
    to parse them as XML. A 404 means the object is not locatable → skip it;
    transport/5xx errors surface via the taxonomy (with retries), never a
    silently-truncated manifest."""
    resp = await _http.request_with_retry(
        client,
        "GET",
        RESOLVE.format(pid=quote(pid, safe="")),
        service="DataONE resolve",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
        follow_redirects=False,
    )
    if resp is None:  # 404 → object not locatable
        return None
    location = resp.headers.get("location")
    if location:  # 303 redirect (live CN behavior)
        return location
    return _first_url(resp.text)  # 200 ObjectLocationList body (legacy / non-redirect)


async def _file_entry(client: httpx.AsyncClient, doc: dict) -> FileEntry | None:
    pid = doc.get("identifier")
    if not pid:
        return None
    url = await _object_url(client, pid)
    if not url:
        return None
    algo, cs = doc.get("checksumAlgorithm"), doc.get("checksum")
    # DataONE reports both "SHA256" and hyphenated "SHA-256"; strip the hyphen so
    # the algo is a valid hashlib name (matches dryad.py) — else fetch.py silently
    # skips verification (unknown algo → no hasher).
    checksum = f"{algo.lower().replace('-', '')}:{cs}" if algo and cs else None
    return FileEntry(
        name=doc.get("fileName") or pid,
        url=url,
        size=doc.get("size"),
        checksum=checksum,
        source="dataone",
    )


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    pid = resource_id.split(":", 1)[1] if resource_id.startswith("dataone:") else resource_id
    _total, docs = await _solr(client, f'identifier:"{pid}"', rows=1, fl=_RESOLVE_FL)
    if not docs:
        raise NotFoundError(f"DataONE has no object {pid!r}")
    resource = _normalize(docs[0])
    rmaps = docs[0].get("resourceMap")
    rmap = rmaps[0] if isinstance(rmaps, list) and rmaps else None
    if not rmap:
        return resource  # metadata-only package
    _t, data_docs = await _solr(
        client, f'resourceMap:"{rmap}" AND formatType:DATA', rows=MAX_SIZE, fl=_DATA_FL
    )
    entries = await asyncio.gather(*[_file_entry(client, d) for d in data_docs])
    return resource.model_copy(update={"files": [e for e in entries if e is not None]})
