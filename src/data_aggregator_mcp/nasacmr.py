"""NASA CMR (Common Metadata Repository / Earthdata) — earth-science collection discovery.

The unit is a CMR *collection* (an Earthdata dataset): ``search`` + ``resolve`` cover the
collection catalog via the keyless UMM-C JSON API.

**Discovery-only.** A CMR collection has no single downloadable file — its data lives in
per-granule files behind an Earthdata login (auth this server does not wire), and the
collection-level "GET DATA" link is an Earthdata Search portal page. So ``files`` is
always empty and ``fetch`` is not offered (the GWAS precedent: a default source that is
in the router but not the fetch gate). ``resolve`` still carries the DOI, provider,
science keywords, and a data-access portal link.

Many collections carry a real DOI (``10.5067/…``), so these records DO participate in
cross-source DOI dedup — unlike the mostly-DOI-less data.gov leg.
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import Creator, DataResource, Link, compact, local_id, year_from

SEARCH = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"
PREFIXES = {"nasacmr"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
_GET = "GET"  # a constant: httpx upper-cases a mutated "get", so the literal would survive
# DataDates types that date the collection itself; UPDATE/REVIEW/DELETE date a revision.
_CREATED_TYPES = ("CREATE", "PRODUCTION")

# An open-content licence URL embedded in the free-text UseConstraints, if any.
_OPEN_URL_RE = re.compile(r"https?://(?:creativecommons\.org|www\.opendefinition\.org)/\S+")
# A collection concept id: "C", digits, "-", the provider id. CMR answers any other
# shape with HTTP 400 ("Concept-id [x] is not valid."), and a granule or service id
# (G…/S…) is not a collection.
_CONCEPT_ID_RE = re.compile(r"C[0-9]+-[A-Za-z0-9_]+")


def _opt(value: object, kind: type) -> bool:
    """Absent or null, or a ``kind``."""
    return value is None or isinstance(value, kind)


def _entries(value: object, fields: tuple[str, ...]) -> bool:
    """Null, or a list whose object entries carry each of ``fields`` as a string or null.
    Entries that are not objects are skipped by the readers, so they pass."""
    return value is None or (
        isinstance(value, list)
        and all(all(_opt(e.get(f), str) for f in fields) for e in value if isinstance(e, dict))
    )


def _is_item(item: object) -> bool:
    """A collection concept id, and every field ``_normalize`` reads at the type it reads
    it as. ``UseConstraints`` is free-form and read defensively, so it is not checked, and
    a DataDates entry's ``Type`` and ``Date`` are read as any type (``str()``,
    ``year_from``), so only the list is."""
    if not isinstance(item, dict):
        return False
    meta, umm = item.get("meta"), item.get("umm")
    if not (isinstance(meta, dict) and isinstance(umm, dict)):
        return False
    cid, doi = meta.get("concept-id"), umm.get("DOI")
    return (
        isinstance(cid, str)
        and _CONCEPT_ID_RE.fullmatch(cid) is not None
        and _opt(meta.get("revision-date"), str)
        and _opt(umm.get("EntryTitle"), str)
        and _opt(umm.get("Abstract"), str)
        and _opt(doi, dict)
        and _opt((doi or {}).get("DOI"), str)
        and _entries(umm.get("DataCenters"), ("ShortName",))
        and _entries(umm.get("ScienceKeywords"), ("Term", "Topic", "Category"))
        and _entries(umm.get("RelatedUrls"), ("URL",))
        and _entries(umm.get("DataDates"), ())
    )


def _check_page(body: dict) -> None:
    """An int hit count and a list of collections (search and resolve read the same
    endpoint). A 200 without them is not "no hits"."""
    items = body.get("items")
    if not (
        type(body.get("hits")) is int
        and isinstance(items, list)
        and all(_is_item(i) for i in items)
    ):
        raise _http.UpstreamEnvelopeError(f"no NASA CMR collection list in {body!r:.200}")


def _strip_doi(raw: str | None) -> str | None:
    """CMR reports DOIs both bare ('10.5067/x') and prefixed ('doi:10.16904/x')."""
    if not raw:
        return None
    v = raw.strip()
    stripped = v[4:] if v.lower().startswith("doi:") else v
    return stripped or None  # a prefix-only "doi:" must yield None, not ""


def _creators(umm: dict) -> list[Creator]:
    """The archiving / distribution data centers stand in as the record's authors."""
    seen: set[str] = set()
    out: list[Creator] = []
    for dc in umm.get("DataCenters") or []:
        if not isinstance(dc, dict):
            continue
        name = (dc.get("ShortName") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(Creator(name=name))
    return out


def _subjects(umm: dict) -> list[str]:
    """The most specific non-null leaf of each science keyword (Term > Topic > Category)."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in umm.get("ScienceKeywords") or []:
        if not isinstance(kw, dict):
            continue
        term = kw.get("Term") or kw.get("Topic") or kw.get("Category")
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def _license_and_access(umm: dict) -> tuple[str | None, str | None]:
    """CMR UseConstraints is free text (often null); pull an embedded CC / public-domain
    URL when present. access='open' only on that positive evidence — NASA data is broadly
    open but licensing varies, so it is never guessed."""
    uc = umm.get("UseConstraints")
    if not isinstance(uc, dict):
        return None, None
    lu = uc.get("LicenseURL")
    for text in (
        lu.get("Linkage") if isinstance(lu, dict) else None,
        uc.get("Description"),
        uc.get("LicenseText"),
    ):
        # A non-string leaf (schema violation) is skipped, not searched. normalize_spdx
        # reads past the ")." that prose puts after a URL.
        m = _OPEN_URL_RE.search(text) if isinstance(text, str) else None
        if m:
            spdx = normalize_spdx(m.group(0))
            return spdx, ("open" if spdx else None)
    return None, None


def _links(umm: dict) -> list[Link]:
    """The primary Earthdata Search portal URL, so resolve points at where the granules
    (and their login-gated download) live."""
    for u in umm.get("RelatedUrls") or []:
        if isinstance(u, dict) and u.get("Type") == "GET DATA" and u.get("URL"):
            return [Link(rel="data_access", target_id=u["URL"])]
    return []


def _pub_year(umm: dict) -> int | None:
    """Publication year from DataDates. Prefer a CREATE/PRODUCTION date — the entries are
    unordered and also carry UPDATE/REVIEW/DELETE, so taking the first parseable one could
    report a revision year as the publication year. Falls back to any parseable date."""
    dates = [dd for dd in umm.get("DataDates") or [] if isinstance(dd, dict)]
    for prefer_created in (True, False):
        for dd in dates:
            if prefer_created and str(dd.get("Type")).upper() not in _CREATED_TYPES:
                continue
            year = year_from(dd.get("Date"))
            if year:
                return year
    return None


def _normalize(item: dict) -> DataResource:
    """Discovery-only: ``files`` stays empty, the granule bytes live behind an Earthdata
    login."""
    meta, umm = item["meta"], item["umm"]
    spdx, access = _license_and_access(umm)
    return DataResource(
        id=f"nasacmr:{meta['concept-id']}",
        source="nasacmr",
        kind="dataset",
        title=umm.get("EntryTitle") or "",
        creators=_creators(umm),
        year=_pub_year(umm),
        description=umm.get("Abstract"),
        doi=_strip_doi((umm.get("DOI") or {}).get("DOI")),
        subjects=_subjects(umm),
        license=spdx,
        access=access,
        last_updated=meta.get("revision-date"),
        links=_links(umm),
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service="NASA CMR search",
        params={"keyword": query, "page_size": str(min(size, MAX_SIZE)), "offset": str(offset)},
        expect=dict,
        check=_check_page,
    )
    return body["hits"], [compact(_normalize(i)) for i in body["items"]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    cid = local_id(resource_id, "nasacmr")
    if not _CONCEPT_ID_RE.fullmatch(cid):
        raise NotFoundError(f"malformed NASA CMR collection id {resource_id!r}")
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service="NASA CMR resolve",
        params={"concept_id": cid},
        expect=dict,
        check=_check_page,
    )
    items = body["items"]
    if not items:
        raise NotFoundError(f"NASA CMR has no collection {cid!r}")
    return _normalize(items[0])
