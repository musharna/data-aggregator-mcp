"""CZ CELLxGENE Discover — single-cell datasets, native adapter.

Search/resolve over the public Discover *curation* REST API. The COLLECTION is the
resource unit (a collection carries one publication DOI and bundles N datasets, each
with H5AD/RDS download assets); modeling the collection avoids DOI-collapse under the
cross-source dedup, since every dataset in a collection shares `collection_doi`.

The curation API has NO server-side search, so `search` fetches the full collections
list (a bare JSON array; ~3 MB, no pagination) and filters client-side over each
collection's name/description/consortia/DOI plus its nested datasets' tissue/disease/
organism/assay ontology labels. `resolve` re-fetches the single collection and flattens
every dataset's assets into files[] (download URLs carry filesize but NO checksum →
unverified fetch, like DANDI/PDB). kind="dataset".
"""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    Link,
    compact,
    normalize_access,
)

API = "https://api.cellxgene.cziscience.com/curation/v1"
_LANDING = "https://cellxgene.cziscience.com/collections/{id}"
PREFIXES = {"cellxgene"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
MANIFEST_CAP = (
    200  # max files surfaced on resolve; huge atlas collections are truncated (documented)
)
_DESC_CAP = 500  # description truncation
DEFAULT_TIMEOUT = 60.0  # the collections list is ~3 MB
MAX_RETRIES = 2

# nested-dataset label fields rolled into the searchable blob + subjects[].
_LABEL_FIELDS = ("tissue", "disease", "assay")


def _labels(collection: dict, field: str) -> list[str]:
    """Unique ontology labels for `field` across a collection's nested datasets."""
    seen: list[str] = []
    for d in collection.get("datasets") or []:
        for term in d.get(field) or []:
            label = term.get("label") if isinstance(term, dict) else None
            if label and label not in seen:
                seen.append(label)
    return seen


def _subjects(collection: dict) -> list[str]:
    out: list[str] = []
    for field in _LABEL_FIELDS:
        for label in _labels(collection, field):
            if label not in out:
                out.append(label)
    return out


def _searchable(collection: dict) -> str:
    parts: list[str] = [
        collection.get("name") or "",
        collection.get("description") or "",
        collection.get("doi") or "",
    ]
    parts += collection.get("consortia") or []
    parts += _labels(collection, "organism")
    parts += _subjects(collection)
    return " ".join(parts).lower()


def _creators(pm: dict) -> list[Creator]:
    out: list[Creator] = []
    for a in pm.get("authors") or []:
        name = a.get("name") or ", ".join(p for p in (a.get("family"), a.get("given")) if p)
        if name:
            out.append(Creator(name=name))
    return out


def _year(collection: dict, pm: dict) -> int | None:
    y = pm.get("published_year")
    if isinstance(y, int):
        return y
    s = collection.get("published_at") or ""
    return int(s[:4]) if s[:4].isdigit() else None


def _links(collection: dict) -> list[Link]:
    cid = collection.get("collection_id") or ""
    out = [
        Link(
            rel="landing_page",
            target_id=collection.get("collection_url") or _LANDING.format(id=cid),
        )
    ]
    for link in collection.get("links") or []:
        url = link.get("link_url")
        if url:
            out.append(Link(rel=(link.get("link_type") or "related").lower(), target_id=url))
    return out


def _truncate(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _DESC_CAP else text[:_DESC_CAP].rstrip() + "…"


def _normalize(collection: dict) -> DataResource:
    cid = collection.get("collection_id") or ""
    pm = collection.get("publisher_metadata") or {}
    return DataResource(
        id=f"cellxgene:{cid}",
        source="cellxgene",
        kind="dataset",
        title=collection.get("name") or cid,
        creators=_creators(pm),
        year=_year(collection, pm),
        description=_truncate(collection.get("description")),
        doi=collection.get("doi") or None,
        organism=_labels(collection, "organism"),
        subjects=_subjects(collection),
        access=normalize_access("open"),
        last_updated=collection.get("revised_at") or collection.get("published_at") or None,
        files=[],
        links=_links(collection),
    )


async def _collections(client: httpx.AsyncClient) -> list[dict]:
    body: Any = await _http.request_json(
        client,
        "GET",
        f"{API}/collections",
        service="CELLxGENE search",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=[],
    )
    return body if isinstance(body, list) else []


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    collections = await _collections(client)
    terms = [t for t in query.lower().split() if t]
    matched = [c for c in collections if all(t in _searchable(c) for t in terms)]
    capped = min(size, MAX_SIZE)
    window = matched[offset : offset + capped] if capped else matched
    return len(matched), [compact(_normalize(c)) for c in window]


def _file_manifest(collection: dict) -> list[FileEntry]:
    out: list[FileEntry] = []
    for d in collection.get("datasets") or []:
        title = d.get("title") or d.get("dataset_id") or ""
        for a in d.get("assets") or []:
            url = a.get("url")
            if not url:
                continue
            ext = (a.get("filetype") or "").lower()
            name = f"{title}.{ext}" if title and ext else url.rsplit("/", 1)[-1]
            out.append(FileEntry(name=name, size=a.get("filesize"), url=url, source="cellxgene"))
            if len(out) >= MANIFEST_CAP:
                return out
    return out


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    cid = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not cid:
        raise NotFoundError(f"malformed CELLxGENE id {resource_id!r}")
    collection = await _http.request_json(
        client,
        "GET",
        f"{API}/collections/{cid}",
        service="CELLxGENE resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if not collection or not collection.get("collection_id"):
        raise NotFoundError(f"CELLxGENE has no collection {cid}")
    record = _normalize(collection)
    record.files = _file_manifest(collection)
    return record
