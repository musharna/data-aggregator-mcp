"""BioStudies (EBI) — functional-genomics studies, including the ArrayExpress collection.

EBI's counterpart to GEO, and until now reachable only indirectly via OmicsDI.
Free-text search spans every collection (ArrayExpress, BioImages, S-EPMC
supplementary sets); ``collection=`` narrows to one.

Three properties earn this source its keep beyond raw coverage:

* **Cross-references feed `relate`.** A study's ``links`` carry sibling accessions
  (GEO ``GSE…``, ENA ``PRJ…``), which we surface in ``accessions`` — so an
  ArrayExpress record connects to the GEO record the router already indexes, with
  the literal shared value as evidence.
* **Publications carry a DOI**, wired into ``doi`` for the paper<->data bridge and
  for cross-source DOI dedup.
* **Files are enumerable**, nested arbitrarily deep in ``section.subsections``.

FETCH IS UNVERIFIED. The API exposes no md5/sha256 for study files (checked
against the live payload 2026-07-21), so ``FileEntry.checksum`` is None and the
catalog says so. `fetch` still streams and still fails loud on an HTML error body;
it just cannot make the integrity claim that Zenodo/ENA fetches make.

Total counts are ESTIMATES on the cross-collection search: the API returns
``isTotalHitsExact: false`` there (consecutive pages reported 1549 then 1550), and
true only within a single collection. We pass the number through as given rather
than implying a precision the source does not have. kind="study".
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, FileEntry, Link, Metrics, compact

SEARCH = "https://www.ebi.ac.uk/biostudies/api/v1/search"
COLLECTION_SEARCH = "https://www.ebi.ac.uk/biostudies/api/v1/{collection}/search"
STUDY = "https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}"
_FILES = "https://www.ebi.ac.uk/biostudies/files/{acc}/{path}"
_LANDING = "https://www.ebi.ac.uk/biostudies/studies/{acc}"
PREFIXES = {"biostudies"}
# BioStudies accessions: E-MTAB-12595, E-GEOD-30436, S-EPMC9542112, S-BSST123.
# Also guards resolve's user-supplied id from path traversal before it hits a URL.
_ACC_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
DEFAULT_SIZE = 10
MAX_SIZE = 100
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

#: Cross-reference link types worth promoting into ``accessions`` so ``relate``
#: and dedup can see them. Anything else stays out — a bad accession is worse
#: than a missing one, because relate reports it as hard evidence.
_XREF_TYPES = {"geo", "ena", "arrayexpress", "pride", "biostudies", "sra", "bioproject"}

#: Attribute names carrying a publication DOI, lowercased for lookup.
_DOI_KEYS = {"doi"}


def _attrs(node: dict[str, Any]) -> dict[str, str]:
    """Flatten an ``attributes`` list into a name->value dict (last wins)."""
    out: dict[str, str] = {}
    for a in node.get("attributes") or []:
        if isinstance(a, dict) and a.get("name"):
            out[str(a["name"])] = str(a.get("value") or "")
    return out


def _iter_subsections(node: Any) -> Any:
    """Yield every subsection dict, at any depth.

    BioStudies nests ``subsections`` arbitrarily and sometimes wraps them in
    lists-of-lists, so a flat scan of section.subsections misses most of them.
    """
    if isinstance(node, list):
        for item in node:
            yield from _iter_subsections(item)
    elif isinstance(node, dict):
        yield node
        yield from _iter_subsections(node.get("subsections") or [])


def _collect_files(section: dict[str, Any], acc: str) -> list[FileEntry]:
    """Walk the section tree and flatten every file entry.

    ``files`` is frequently a list OF LISTS, and lives on nested subsections
    rather than the top-level section.
    """
    out: list[FileEntry] = []
    seen: set[str] = set()
    for node in _iter_subsections(section):
        raw = node.get("files") or []
        stack: list[Any] = [raw]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
                continue
            if not isinstance(cur, dict):
                continue
            path = cur.get("path") or cur.get("name")
            if not path or path in seen:
                continue
            seen.add(str(path))
            size = cur.get("size")
            out.append(
                FileEntry(
                    name=str(path),
                    size=int(size) if isinstance(size, int) else None,
                    url=_FILES.format(acc=acc, path=str(path)),
                    # No checksum in the payload — see module docstring. Leaving
                    # this None is what keeps fetch honest about the guarantee.
                    checksum=None,
                    source="biostudies",
                )
            )
    return out


def _publication(section: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (doi, pmid) from the Publication subsection, if present."""
    doi = pmid = None
    for node in _iter_subsections(section):
        if str(node.get("type") or "").lower() != "publication":
            continue
        attrs = _attrs(node)
        for key, val in attrs.items():
            low = key.lower()
            if low in _DOI_KEYS and val and not doi:
                doi = val
            elif low in {"pmid", "pubmed", "pubmedid"} and val and not pmid:
                pmid = val
        # The Publication subsection's own accno is often the PMID.
        accno = node.get("accno")
        if not pmid and accno and str(accno).isdigit():
            pmid = str(accno)
    return doi, pmid


def _xrefs(section: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (type, accession) pairs from the section's links, at any nesting."""
    out: list[tuple[str, str]] = []
    stack: list[Any] = [section.get("links") or []]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, dict):
            continue
        url = cur.get("url")
        if not url:
            continue
        ltype = _attrs(cur).get("Type", "").strip().lower()
        if ltype in _XREF_TYPES:
            out.append((ltype, str(url)))
    return out


def _normalize_hit(hit: dict[str, Any]) -> DataResource:
    """Build a compact record from a search hit (no detail call)."""
    acc = str(hit.get("accession") or "")
    release = str(hit.get("release_date") or "")
    year = int(release[:4]) if release[:4].isdigit() else None
    views = hit.get("views")
    return DataResource(
        id=f"biostudies:{acc}",
        source="biostudies",
        kind="study",
        title=str(hit.get("title") or acc),
        year=year,
        accessions=[acc] if acc else [],
        last_updated=release or None,
        files=[],
        links=[Link(rel="landing_page", target_id=_LANDING.format(acc=acc))],
        metrics=Metrics(views=int(views)) if isinstance(views, int) else None,
    )


def _normalize_study(body: dict[str, Any]) -> DataResource:
    """Build the full record from a study detail payload."""
    acc = str(body.get("accno") or "")
    top = _attrs(body)
    section = body.get("section") or {}
    sec = _attrs(section)

    title = sec.get("Title") or top.get("Title") or acc
    release = top.get("ReleaseDate") or ""
    year = int(release[:4]) if release[:4].isdigit() else None
    organism = [o for o in [sec.get("Organism")] if o]
    collection = top.get("AttachTo")
    study_type = sec.get("Study type")

    doi, pmid = _publication(section)
    identifiers: dict[str, str] = {}
    if pmid:
        identifiers["pmid"] = pmid

    accessions = [acc] if acc else []
    links = [Link(rel="landing_page", target_id=_LANDING.format(acc=acc))]
    for ltype, value in _xrefs(section):
        if value not in accessions:
            accessions.append(value)
        identifiers.setdefault(ltype, value)
    if doi:
        links.append(Link(rel="described_in", target_id=doi))

    subjects = [s for s in (collection, study_type) if s]

    return DataResource(
        id=f"biostudies:{acc}",
        source="biostudies",
        kind="study",
        title=title,
        year=year,
        description=sec.get("Description") or None,
        doi=doi,
        identifiers=identifiers,
        accessions=accessions,
        organism=organism,
        subjects=subjects,
        access="open",  # the search API only surfaces isPublic records
        last_updated=release or None,
        files=_collect_files(section, acc),
        links=links,
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    collection: str | None = None,
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    # The API pages by 1-indexed page number, not row offset.
    page = (offset // capped) + 1 if capped else 1
    url = (
        COLLECTION_SEARCH.format(collection=collection)
        if collection and _ACC_RE.match(collection)
        else SEARCH
    )
    body = await _http.request_json(
        client,
        "GET",
        url,
        service="BioStudies search",
        params={"query": query, "pageSize": capped, "page": page},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    hits = (body or {}).get("hits") or []
    recs = [compact(_normalize_hit(h)) for h in hits if isinstance(h, dict)]
    total = (body or {}).get("totalHits")
    return (int(total) if isinstance(total, int) else len(recs)), recs


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not _ACC_RE.match(acc):
        raise NotFoundError(f"malformed BioStudies id {resource_id!r}")
    try:
        body = await _http.request_json(
            client,
            "GET",
            STUDY.format(acc=acc),
            service="BioStudies resolve",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"BioStudies has no study {acc}") from None
    return _normalize_study(body)
