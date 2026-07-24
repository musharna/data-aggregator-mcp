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
from data_aggregator_mcp.models import Creator, DataResource, Link, compact, year_from

SEARCH = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"
PREFIXES = {"nasacmr"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# An open-content licence URL embedded in the free-text UseConstraints, if any.
_OPEN_URL_RE = re.compile(r"https?://(?:creativecommons\.org|www\.opendefinition\.org)/\S+")


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
    text = " ".join(
        s
        for s in (
            lu.get("Linkage") if isinstance(lu, dict) else None,
            uc.get("Description"),
            uc.get("LicenseText"),
        )
        if isinstance(s, str) and s  # a non-string leaf (schema violation) must not crash the join
    )
    m = _OPEN_URL_RE.search(text)
    spdx = normalize_spdx(m.group(0).rstrip(".)") if m else None)
    return spdx, ("open" if spdx else None)


def _links(umm: dict) -> list[Link]:
    """The primary Earthdata Search portal URL, so resolve points at where the granules
    (and their login-gated download) live."""
    for u in umm.get("RelatedUrls") or []:
        if isinstance(u, dict) and (u.get("Type") or "") == "GET DATA" and u.get("URL"):
            return [Link(rel="data_access", target_id=u["URL"])]
    return []


def _pub_year(umm: dict) -> int | None:
    """Publication year from DataDates. Prefer a CREATE/PRODUCTION date — the entries are
    unordered and also carry UPDATE/REVIEW/DELETE, so taking the first parseable one could
    report a revision year as the publication year. Falls back to any parseable date."""
    dates = [dd for dd in umm.get("DataDates") or [] if isinstance(dd, dict)]
    for prefer_created in (True, False):
        for dd in dates:
            if prefer_created and (dd.get("Type") or "").upper() not in ("CREATE", "PRODUCTION"):
                continue
            year = year_from(dd.get("Date"))
            if year:
                return year
    return None


def _normalize(item: dict) -> DataResource:
    meta = item.get("meta") or {}
    umm = item.get("umm") or {}
    spdx, access = _license_and_access(umm)
    return DataResource(
        id=f"nasacmr:{meta.get('concept-id') or ''}",
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
        files=[],  # discovery-only: granule bytes live behind an Earthdata login
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="NASA CMR search",
        params={"keyword": query, "page_size": str(min(size, MAX_SIZE)), "offset": str(offset)},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    total = int(body.get("hits") or 0)
    return total, [compact(_normalize(i)) for i in (body.get("items") or [])]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    cid = resource_id.split(":", 1)[1] if resource_id.startswith("nasacmr:") else resource_id
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="NASA CMR resolve",
        params={"concept_id": cid},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    items = body.get("items") or []
    if not items:
        raise NotFoundError(f"NASA CMR has no collection {cid!r}")
    return _normalize(items[0])
