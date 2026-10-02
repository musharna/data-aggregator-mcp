"""DataONE federation (eco/environmental) — search + resolve + verified fetch.

Discovery hits the Coordinating Node Solr index. Data bytes live on Member
Nodes, so ``resolve`` does a per-object ``/resolve/`` hop (Task 4) to get the
streamable MN url. Checksums vary per object (MD5 or SHA256); the prefix is
built from ``checksumAlgorithm`` so ``fetch.py``'s ``_hasher`` verifies either.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx
from defusedxml import ElementTree as ET  # remote XML: entity-expansion safe
from defusedxml.common import DefusedXmlException

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    compact,
    local_id,
    year_from,
)

logger = logging.getLogger(__name__)

SOLR = "https://cn.dataone.org/cn/v2/query/solr/"
RESOLVE = "https://cn.dataone.org/cn/v2/resolve/{pid}"
PREFIXES = {"dataone"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
# Most data objects attached to one resolved package (each needs its own CN resolve).
MANIFEST_CAP = 1000
# httpx upper-cases the method and reads header names case-insensitively, so a
# spelling mutant of either sends the same request.
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
# A PID is one path segment: quote() escapes "/" too.
_SEGMENT_SAFE = ""
# The CN answers 303 to the Member Node; following it would fetch the object bytes
# instead of the locator (pinned by test_object_url_303_returns_location_without_following).
_UNFOLLOWED = False

# Lucene special characters that must be backslash-escaped when user-supplied
# strings are interpolated into a Solr query (boolean operators && and || are
# handled by escaping & and | which already appear in the set below).
_LUCENE_SPECIALS = r'\+-&&||!(){}[]^"~*?:/'

_SEARCH_FL = (
    "identifier,title,author,origin,formatId,dateUploaded,datePublished,dateModified,resourceMap"
)
_RESOLVE_FL = "identifier,title,author,origin,dateUploaded,datePublished,dateModified,resourceMap"
_DATA_FL = "identifier,fileName,size,checksum,checksumAlgorithm"
# Latest version only, as DataONE's own search UI does (MetacatUI ``Search.js``
# excludes ``obsoletedBy:*``): every update leaves the old version indexed, and
# 108,568 of 110,671 live "salmon" hits were superseded copies (2026-10-01).
_SEARCH_FILTER = " AND formatType:METADATA AND -obsoletedBy:*"
# Fields read as text and as lists of text; ``size`` is the one number.
_TEXT_FIELDS = (
    "title",
    "author",
    "dateUploaded",
    "datePublished",
    "dateModified",
    "fileName",
    "checksum",
    "checksumAlgorithm",
)
_TEXT_LIST_FIELDS = ("origin", "resourceMap")


def _escape_lucene(value: str) -> str:
    """Backslash-escape Lucene/Solr special characters in a user-supplied string.

    This prevents a caller-controlled value from restructuring a boolean query
    when the escaped value is interpolated into a Solr ``q`` expression.
    """
    out: list[str] = []
    for ch in value:
        if ch in _LUCENE_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _is_doc(doc: object) -> bool:
    """A non-empty identifier, and every other field the readers use has the type they
    read it as (absent is fine)."""
    if not (isinstance(doc, dict) and isinstance(doc.get("identifier"), str) and doc["identifier"]):
        return False
    lists = [doc.get(k) for k in _TEXT_LIST_FIELDS]
    size = doc.get("size")
    return (
        all(isinstance(doc.get(k), str | None) for k in _TEXT_FIELDS)
        and all(
            v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v)) for v in lists
        )
        and (size is None or type(size) is int)
    )


def _check_solr(body: dict) -> None:
    resp = body.get("response")
    docs = resp.get("docs") if isinstance(resp, dict) else None
    found = resp.get("numFound") if isinstance(resp, dict) else None
    if not (isinstance(docs, list) and all(_is_doc(d) for d in docs) and type(found) is int):
        raise _http.UpstreamEnvelopeError(f"no DataONE result list in {body!r:.200}")


def _creators(doc: dict) -> list[Creator]:
    origin = doc.get("origin")
    if origin:
        return [Creator(name=o) for o in origin if o]
    author = doc.get("author")
    return [Creator(name=author)] if author else []


def _normalize(doc: dict) -> DataResource:
    pid = doc["identifier"]
    doi = pid[4:] if pid[:4].lower() == "doi:" else None
    return DataResource(
        id=f"dataone:{pid}",
        source="dataone",
        kind="dataset",
        title=doc.get("title") or "",
        creators=_creators(doc),
        year=year_from(doc.get("datePublished"), doc.get("dateUploaded")),
        last_updated=doc.get("dateModified"),
        doi=doi,
    )


async def _solr(
    client: httpx.AsyncClient, query: str, *, rows: int, fl: str, start: int = 0
) -> tuple[int, list[dict]]:
    body = await _http.request_json(
        client,
        _GET,
        SOLR,
        service="DataONE search",
        params={"q": query, "fl": fl, "rows": str(rows), "start": str(start), "wt": "json"},
        headers=_ACCEPT_JSON,
        expect=dict,
        check=_check_solr,
    )
    return body["response"]["numFound"], body["response"]["docs"]


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    q = f"({_escape_lucene(query)}){_SEARCH_FILTER}"
    total, docs = await _solr(client, q, rows=capped, start=offset, fl=_SEARCH_FL)
    return total, [compact(_normalize(d)) for d in docs]


def _first_url(xml_text: str) -> str | None:
    """First <url> in a DataONE ObjectLocationList (namespace-agnostic), or None."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException):
        # a DOCTYPE/entity payload is 'unparseable' too; defusedxml raises a
        # ValueError subclass for it, which ParseError alone would let escape
        return None
    for el in root.iter():
        if (el.tag == "url" or el.tag.endswith("}url")) and el.text:
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
        _GET,
        RESOLVE.format(pid=quote(pid, safe=_SEGMENT_SAFE)),
        service="DataONE resolve",
        not_found_returns=None,
        follow_redirects=_UNFOLLOWED,
    )
    if resp is None:  # 404 → object not locatable
        return None
    # Header names are case-insensitive, so a spelling mutant reads the same header.
    location = resp.headers.get("location")  # pragma: no mutate
    if location:  # 303 redirect (live CN behavior)
        return location
    return _first_url(resp.text)  # 200 ObjectLocationList body (legacy / non-redirect)


async def _file_entry(client: httpx.AsyncClient, doc: dict) -> FileEntry | None:
    pid = doc["identifier"]
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


async def _package_data_docs(client: httpx.AsyncClient, q: str) -> list[dict]:
    """Every data object in a package, paged ``MAX_SIZE`` rows at a time until
    ``numFound``. One unpaged ``rows=50`` query silently dropped the rest of a larger
    package. Bounded by ``MANIFEST_CAP`` (each object costs a CN resolve round-trip);
    truncation there is logged, never silent — mirroring cellxgene's manifest cap."""
    docs: list[dict] = []
    start = 0
    while True:
        total, page = await _solr(client, q, rows=MAX_SIZE, start=start, fl=_DATA_FL)
        docs.extend(page)
        start += len(page)
        if not page or start >= total:
            return docs
        if len(docs) >= MANIFEST_CAP:
            logger.warning(
                "DataONE package %s holds %d data objects; attaching the first %d",
                q,
                total,
                MANIFEST_CAP,
            )
            return docs[:MANIFEST_CAP]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    pid = local_id(resource_id, "dataone")
    _escaped_pid = _escape_lucene(pid)
    _total, docs = await _solr(client, f'identifier:"{_escaped_pid}"', rows=1, fl=_RESOLVE_FL)
    if not docs:
        raise NotFoundError(f"DataONE has no object {pid!r}")
    resource = _normalize(docs[0])
    rmaps = docs[0].get("resourceMap")
    rmap = rmaps[0] if rmaps else None
    if not rmap:
        return resource  # metadata-only package
    _escaped_rmap = _escape_lucene(rmap)
    data_docs = await _package_data_docs(
        client, f'resourceMap:"{_escaped_rmap}" AND formatType:DATA'
    )
    entries = await asyncio.gather(*[_file_entry(client, d) for d in data_docs])
    return resource.model_copy(update={"files": [e for e in entries if e is not None]})
