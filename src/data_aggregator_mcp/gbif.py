"""GBIF (Global Biodiversity Information Facility) — dataset search + resolve + fetch.

The unit is a GBIF **dataset** (occurrence / checklist / sampling-event / metadata),
which carries a DOI, a machine-readable licence, and — for archive-backed types — a
downloadable Darwin Core Archive. Occurrence-level records and filtered/derived
download DOIs (async, auth-gated, checksummed) are deliberately out of scope for this
adapter; each is a separable follow-up.

Search hits the registry dataset-search index (``/v1/dataset/search``); resolve pulls
the full registry record (``/v1/dataset/{key}``) and attaches the ``DWC_ARCHIVE``
endpoint as a fetchable file. GBIF exposes no checksum for the dataset archive, so
fetch is UNVERIFIED (``fetch.py``'s content sniff still rejects an HTML error page
served as a zip) — mirroring the HuggingFace precedent.

Dedup note: GBIF DOIs use the ``10.15468`` prefix, which is a DataCite prefix, so
DataCite also indexes them. This adapter is registered BEFORE ``datacite`` in the
router so the fetchable GBIF record wins the DOI collision in ``_dedup``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    compact,
    local_id,
    strip_html,
    year_from,
)

SEARCH = "https://api.gbif.org/v1/dataset/search"
DATASET = "https://api.gbif.org/v1/dataset/{key}"
PREFIXES = {"gbif"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
# httpx upper-cases the method, so a lower-cased literal would be an equivalent mutant.
_GET = "GET"

# contact.type values that denote authorship, as opposed to administrative /
# technical points of contact (which are noise in a creators[] list).
_AUTHOR_CONTACT_TYPES = {
    "ORIGINATOR",
    "METADATA_AUTHOR",
    "PRINCIPAL_INVESTIGATOR",
    "AUTHOR",
}

# The string fields the readers below use; each may be absent or null.
_TEXT_FIELDS = (
    "title",
    "description",
    "doi",
    "license",
    "pubDate",
    "publicationDate",
    "created",
    "modified",
    "publishingOrganizationTitle",
    "deleted",
)
_CONTACT_FIELDS = ("type", "firstName", "lastName", "organization")
_ENDPOINT_FIELDS = ("type", "url")


def _texts(item: object, keys: tuple[str, ...]) -> bool:
    return isinstance(item, dict) and all(isinstance(item.get(k), str | None) for k in keys)


def _items(value: object, ok: Callable[[Any], bool]) -> bool:
    return value is None or (isinstance(value, list) and all(ok(v) for v in value))


def _is_keyword(k: object) -> bool:
    return isinstance(k, str | None)


def _is_dataset(doc: object) -> bool:
    """A non-empty string key, and every other field the readers use at the type they
    read it as (absent or null is fine)."""
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("key"), str)
        and bool(doc["key"])
        and _texts(doc, _TEXT_FIELDS)
        and _items(doc.get("contacts"), lambda c: _texts(c, _CONTACT_FIELDS))
        and _items(doc.get("keywords"), _is_keyword)
        and _items(
            doc.get("keywordCollections"),
            lambda c: isinstance(c, dict) and _items(c.get("keywords"), _is_keyword),
        )
        and _items(doc.get("endpoints"), lambda e: _texts(e, _ENDPOINT_FIELDS))
    )


def _check_dataset(body: dict) -> None:
    if not _is_dataset(body):
        raise _http.UpstreamEnvelopeError(f"no GBIF dataset in {body!r:.200}")


def _check_hits(body: dict) -> None:
    results = body.get("results")
    if not (
        isinstance(results, list)
        and all(_is_dataset(d) for d in results)
        and type(body.get("count")) is int
    ):
        raise _http.UpstreamEnvelopeError(f"no GBIF dataset list in {body!r:.200}")


def _access_from_spdx(spdx: str | None) -> str | None:
    """GBIF datasets are openly downloadable under CC / CC0 (an NC clause restricts
    reuse, not access), so map a recognized CC licence to ``open``. An unrecognized or
    absent licence stays None — never a guessed access level."""
    return "open" if spdx and spdx.startswith("CC") else None


def _dedup(values: list[str]) -> list[str]:
    """Non-empty values in first-seen order, each once."""
    return list(dict.fromkeys(v for v in values if v))


def _creators(doc: dict) -> list[Creator]:
    """Authorship from the resolve record's ``contacts[]`` (people or their org),
    falling back to ``publishingOrganizationTitle`` — GBIF cites datasets by their
    publishing organization, so it is the citation author when no contact is named.
    The search index carries no contacts, so search results get the publisher."""
    names: list[str] = []
    for c in doc.get("contacts") or []:
        if str(c.get("type")).upper() not in _AUTHOR_CONTACT_TYPES:
            continue
        person = " ".join(
            p for p in ((c.get("firstName") or "").strip(), (c.get("lastName") or "").strip()) if p
        )
        names.append(person or (c.get("organization") or "").strip())
    org = (doc.get("publishingOrganizationTitle") or "").strip()
    return [Creator(name=n) for n in _dedup(names) or _dedup([org])]


def _subjects(doc: dict) -> list[str]:
    """Keywords: a flat ``keywords`` list on the search index, or the structured
    ``keywordCollections`` on the resolve record. Order-preserving dedup."""
    flat: list[str] = list(doc.get("keywords") or [])
    for coll in doc.get("keywordCollections") or []:
        flat.extend(coll.get("keywords") or [])
    return _dedup(flat)


def _normalize(doc: dict) -> DataResource:
    spdx = normalize_spdx(doc.get("license"))
    return DataResource(
        id=f"gbif:{doc['key']}",
        source="gbif",
        kind="dataset",
        title=doc.get("title") or "",
        creators=_creators(doc),
        # The dataset record says ``pubDate``; the search index says ``publicationDate``.
        year=year_from(doc.get("pubDate"), doc.get("publicationDate"), doc.get("created")),
        description=strip_html(doc.get("description")),
        doi=doc.get("doi"),
        subjects=_subjects(doc),
        license=spdx,
        access=_access_from_spdx(spdx),
        last_updated=doc.get("modified"),
    )


def _archive_files(doc: dict) -> list[FileEntry]:
    """The Darwin Core Archive endpoint(s) — a direct-download zip with no upstream
    checksum, so ``checksum`` is left None and fetch runs unverified. Non-archive
    endpoints (EML metadata, feeds) are not fetch targets and are skipped."""
    return [
        FileEntry(
            name=f"{doc['key']}.dwca.zip", url=ep["url"], mime="application/zip", source="gbif"
        )
        for ep in doc.get("endpoints") or []
        if str(ep.get("type")).upper() == "DWC_ARCHIVE" and ep.get("url")
    ]


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service="GBIF dataset search",
        params={"q": query, "limit": str(min(size, MAX_SIZE)), "offset": str(offset)},
        expect=dict,
        check=_check_hits,
    )
    return body["count"], [compact(_normalize(d)) for d in body["results"]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    # Every dataset key is a UUID, and GBIF answers anything else with HTTP 400, which
    # would surface as an outage. Parsing it also keeps the path to one segment.
    try:
        key = str(uuid.UUID(local_id(resource_id, "gbif")))
    except ValueError:
        raise NotFoundError(f"malformed GBIF dataset key {resource_id!r} (not a UUID)") from None
    doc = await _http.request_json(
        client,
        _GET,
        DATASET.format(key=key),
        service="GBIF dataset",
        not_found_returns=None,
        expect=dict,
        check=_check_dataset,
    )
    if doc is None:
        raise NotFoundError(f"GBIF has no dataset {key!r}")
    if doc.get("deleted"):
        # The registry keeps a deleted dataset's record (HTTP 200) with the deletion
        # time; its archive URL no longer serves the data.
        raise NotFoundError(f"GBIF dataset {key!r} was deleted on {doc['deleted']}")
    return _normalize(doc).model_copy(update={"files": _archive_files(doc)})
