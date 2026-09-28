"""DataCite archives discovery adapter — async wrapper around api.datacite.org.

Discovery: GET /dois?query=...  (one query spans every DataCite client —
Dryad, Zenodo, Figshare, Dataverse, OSF, Mendeley, ...).
Resolve:   GET /dois/{doi}

DataCite metadata itself carries no file manifest, so ``_normalize`` returns
files=[]. ``resolve`` then attaches files[] by dispatching on the detected
``source`` to each host repo's native API via ``_FILE_RESOLVERS`` (Figshare,
Dataverse, OSF — fetchable; Dryad — manifest-only). A Zenodo DOI is delegated to
``zenodo.resolve`` so its files[] populate from the native adapter — one in Zenodo's
own 10.5281 namespace skips the DataCite GET entirely, the rest delegate after it;
unrecognized repos stay files=[]. Fetchability is enforced post-resolve by the
server fetch guard.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from data_aggregator_mcp import (
    _http,
    _pushdown,
    dataverse,
    dryad,
    figshare,
    openneuro,
    osf,
    zenodo,
)
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import host_matches
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    FundingRef,
    Link,
    Metrics,
    _orcid,
    _rel,
    compact,
    local_id,
)

BASE_URL = "https://api.datacite.org"
PREFIXES = frozenset({"datacite"})  # bare DOIs (containing '/') also route here (router.resolve)
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_SIZE = 10
MAX_SIZE = 50

# DataCite types.resourceTypeGeneral → DataResource.kind
_KIND_MAP = {
    "Dataset": "dataset",
    "Collection": "dataset",
    "Software": "software",
    "ComputationalNotebook": "software",
    "Text": "publication",
    "JournalArticle": "publication",
    "Preprint": "publication",
    "Book": "publication",
    "BookChapter": "publication",
    "ConferencePaper": "publication",
    "Report": "publication",
    "Dissertation": "publication",
}

# relationships.client.data.id → friendly source name (matched by substring).
# publisher is unreliable (figshare.ars → "Taylor & Francis"), so we key on the
# client id. Falls back to the raw client id so source is never wrong, only
# less friendly.
_SOURCE_RULES = (
    ("zenodo", "zenodo"),
    ("dryad", "dryad"),
    ("figshare", "figshare"),
    ("dataverse", "dataverse"),
    ("gdcc", "dataverse"),  # Harvard etc. surface as gdcc.* (no "dataverse" substring)
    ("osf", "osf"),
    ("mendeley", "mendeley"),
    ("openneuro", "openneuro"),
)


def _source_for_client(client_id: str) -> str:
    cid = client_id.lower()
    for needle, name in _SOURCE_RULES:
        if needle in cid:
            return name
    return client_id


# source name → per-repo file-manifest resolver (populates files[] at resolve).
# Dryad is included (manifest-only); fetchability is gated separately in server.py.
_FILE_RESOLVERS: dict[str, Callable[..., Awaitable[list[FileEntry]]]] = {
    "dryad": dryad.files,
    "figshare": figshare.files,
    "dataverse": dataverse.files,
    "osf": osf.files,
    "openneuro": openneuro.files,
}

# File resolvers whose host is a federation of installations: they take the record's
# DataCite landing URL (``landing_url=``) to pick the installation that holds it.
_LANDING_AWARE = frozenset({"dataverse"})

# Zenodo's own DOI namespace. `resolve` returns the native Zenodo record for these
# anyway, so the DataCite GET is pure latency — match on the requested DOI and skip
# it. Deliberately narrow: a Zenodo record minted under any other prefix still takes
# the fetch-then-delegate path in `resolve`, so this changes cost, not coverage.
_ZENODO_DOI_RE = re.compile(r"10\.5281/zenodo\.(\d+)", re.IGNORECASE)


def _first(items: list[dict[str, Any]] | None, key: str) -> str | None:
    if not items:
        return None
    return items[0].get(key)


def _year(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


# rightsList mixes two kinds of entry: licences, and an access STATUS from the
# info:eu-repo vocabulary (Harvard Dataverse, DataverseNO and Mendeley list it first,
# with rights/rightsIdentifier null). A status is never a licence.
_EU_REPO_ACCESS = {
    "info:eu-repo/semantics/openaccess": "open",
    "info:eu-repo/semantics/embargoedaccess": "embargoed",
    "info:eu-repo/semantics/restrictedaccess": "restricted",
    "info:eu-repo/semantics/closedaccess": "closed",
}


def _access_status(r: dict[str, Any]) -> str | None:
    """The access status an info:eu-repo rights entry states; None for any other entry."""
    for field in ("rightsUri", "rights"):
        status = _EU_REPO_ACCESS.get((r.get(field) or "").strip().lower())
        if status:
            return status
    return None


def _license_from_rights(rights_list: list[dict[str, Any]]) -> str | None:
    """The first entry that is a licence (not an access status): its identifier, else
    its name."""
    for r in rights_list:
        if _access_status(r) is None and (r.get("rightsIdentifier") or r.get("rights")):
            return r.get("rightsIdentifier") or r.get("rights")
    return None


def _access_from_rights(rights_list: list[dict[str, Any]] | None) -> str | None:
    """DataCite has no access field. An info:eu-repo access status, when listed, is the
    answer — an embargoed record with a CC licence is embargoed, not open. Otherwise
    infer 'open' from an open-content license (Creative Commons / public domain), and
    else None — do not guess."""
    for r in rights_list or []:
        status = _access_status(r)
        if status:
            return status
    for r in rights_list or []:
        ident = (r.get("rightsIdentifier") or "").lower()
        uri = (r.get("rightsUri") or "").lower()
        # Host check, not substring: "creativecommons.org" appearing anywhere in a
        # URI also matches http://paywall.example.com/creativecommons.org, which
        # would flip a closed record to access="open".
        if (
            ident.startswith("cc")
            or host_matches(uri, "creativecommons.org")
            or "publicdomain" in uri
        ):
            return "open"
    return None


def _metrics(a: dict[str, Any]) -> Metrics | None:
    """Pull DataCite's inline usage counts. Returns None when none are present
    so the field stays absent rather than a zero-filled object."""
    cites, views, dls = a.get("citationCount"), a.get("viewCount"), a.get("downloadCount")
    if cites is None and views is None and dls is None:
        return None
    return Metrics(citations=cites, views=views, downloads=dls)


def _creator(c: dict[str, Any]) -> Creator:
    orcid = None
    for nid in c.get("nameIdentifiers") or []:
        ident = nid.get("nameIdentifier") or ""
        scheme = (nid.get("nameIdentifierScheme") or "").upper()
        # Only treat it as an ORCID when the source SAYS so — an ISNI/GND id can
        # match the ORCID shape, so the regex alone is not sufficient evidence.
        if scheme != "ORCID" and "orcid.org" not in ident.lower():
            continue
        cand = _orcid(ident)
        if cand:
            orcid = cand
            break
    return Creator(name=c.get("name", ""), orcid=orcid)


def _normalize(item: dict[str, Any]) -> DataResource:
    a = item.get("attributes", {}) or {}
    client_id = (((item.get("relationships") or {}).get("client") or {}).get("data") or {}).get(
        "id", ""
    )
    rt = (a.get("types") or {}).get("resourceTypeGeneral", "")
    rights = a.get("rightsList") or []
    license_ = _license_from_rights(rights)
    doi = a.get("doi")
    return DataResource(
        id=f"datacite:{doi}",
        source=_source_for_client(client_id),
        kind=_KIND_MAP.get(rt, "dataset"),
        title=_first(a.get("titles"), "title") or "",
        creators=[_creator(c) for c in (a.get("creators") or [])],
        funding=[
            FundingRef(funder=f["funderName"], award=f.get("awardNumber") or f.get("awardTitle"))
            for f in (a.get("fundingReferences") or [])
            if f.get("funderName")
        ],
        year=_year(a.get("publicationYear")),
        description=_first(a.get("descriptions"), "description"),
        doi=doi,
        subjects=[s.get("subject", "") for s in (a.get("subjects") or []) if s.get("subject")],
        license=license_,
        access=_access_from_rights(rights),
        metrics=_metrics(a),
        last_updated=a.get("updated"),
        links=[
            Link(rel=_rel(r["relationType"]), target_id=r["relatedIdentifier"])
            for r in (a.get("relatedIdentifiers") or [])
            if r.get("relationType") and r.get("relatedIdentifier")
        ],
        files=[],  # DataCite is metadata-only
    )


def _filter_clauses(filters: Mapping[str, Any]) -> list[str]:
    """DataCite query clauses for the pushable facet filters. ``publicationYear`` is the
    same field ``_normalize`` reads ``year`` from, so the bound is identical."""
    pa, pb = filters.get("published_after"), filters.get("published_before")
    clauses = [
        _pushdown.range_clause(
            "publicationYear",
            str(pa) if pa is not None else None,
            str(pb) if pb is not None else None,
        )
    ]
    if (kind := filters.get("kind")) is not None:
        clauses.append(_pushdown.kind_clause("types.resourceTypeGeneral", _KIND_MAP, kind))
    return [c for c in clauses if c is not None]


def pushable(filters: Mapping[str, Any], /) -> dict[str, Any]:
    """The active filters DataCite can evaluate: both year bounds, and any ``kind`` a
    ``resourceTypeGeneral`` normalizes to (``_pushdown.FilterPushdown``)."""
    act = _pushdown.active(filters)
    out = {k: v for k, v in act.items() if k in _pushdown.YEAR_FILTERS}
    kind = act.get("kind")
    if kind is not None and _pushdown.kind_clause("types.resourceTypeGeneral", _KIND_MAP, kind):
        out["kind"] = kind
    return out


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    filters: Mapping[str, Any] | None = None,
) -> tuple[int, list[DataResource]]:
    """Search DataCite DOIs. Returns (total_hits, COMPACT resources).
    ``offset`` → page ``offset // size + 1`` then drop first ``offset % size``.

    ``filters`` (the subset :func:`pushable` accepted) are ANDed onto ``query``, so
    DataCite evaluates them and ``total_hits`` is the filtered total. None/empty sends
    ``query`` unchanged."""
    capped = min(size, MAX_SIZE)
    q = _pushdown.with_clauses(query, _filter_clauses(filters or {}))
    params = {"query": q, "page[size]": str(capped)}
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page[number]"] = str(offset // capped + 1)
    body = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/dois",
        service="DataCite search",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    items = (body.get("data", []) or [])[offset % capped :]
    total = int((body.get("meta") or {}).get("total", len(items)))
    return total, [compact(_normalize(it)) for it in items]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve a DataCite DOI (``datacite:10.x/y`` or a bare ``10.x/y``).

    DataCite returns a single record under ``data`` (a dict, not a list).
    """
    doi = local_id(resource_id, "datacite")
    native = _ZENODO_DOI_RE.fullmatch(doi)
    if native is not None:  # skip the DataCite round-trip we would only discard
        return await zenodo.resolve(client, f"zenodo:{native.group(1)}")
    try:
        body = await _http.request_json(
            client,
            "GET",
            f"{BASE_URL}/dois/{_http.doi_path(doi)}",
            service="DataCite resolve",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"DataCite has no DOI {doi!r}") from None
    data = body.get("data")
    if not isinstance(data, dict):
        raise NotFoundError(
            f"DataCite returned a malformed response for {doi!r} (missing 'data' key)"
        )
    resource = _normalize(data)
    if resource.source == "zenodo" and resource.doi and "zenodo." in resource.doi:
        recid = resource.doi.rsplit("zenodo.", 1)[-1]
        if recid.isdigit():
            return await zenodo.resolve(client, f"zenodo:{recid}")
    resolver = _FILE_RESOLVERS.get(resource.source)
    if resolver is not None and resource.doi:
        if resource.source in _LANDING_AWARE:
            # The host repo is a federation (Dataverse): the record's own landing URL
            # names the installation that holds it. Any other server cannot resolve it.
            landing = (data.get("attributes") or {}).get("url")
            file_list = await resolver(client, resource.doi, landing_url=landing)
        else:
            file_list = await resolver(client, resource.doi)
        if file_list:
            resource = resource.model_copy(update={"files": file_list})
    return resource
