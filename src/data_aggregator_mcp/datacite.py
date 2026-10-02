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
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
DEFAULT_SIZE = 10
MAX_SIZE = 50

# DataCite types.resourceTypeGeneral → DataResource.kind
_KIND_FIELD = "types.resourceTypeGeneral"
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
# A Zenodo record minted under another prefix ends in zenodo.<record number>.
_ZENODO_RECID_RE = re.compile(r"zenodo\.(\d+)$")


def _main(items: list[dict[str, Any]] | None, key: str, type_key: str, main: str | None) -> Any:
    """The entry of the main type (titles: untyped; descriptions: ``Abstract``), else the
    first. Records list subtitles, translated titles, contact blocks and series lines in
    any order, so the first entry is not the title or the abstract (probe 2026-10-01)."""
    if not items:
        return None
    return next((i for i in items if i.get(type_key) == main), items[0]).get(key)


# The attribute lists `_normalize` reads, and the text fields read off their entries.
_LIST_FIELDS = {
    "titles": ("title", "titleType"),
    "descriptions": ("description", "descriptionType"),
    "creators": ("name", "givenName", "familyName"),
    "subjects": ("subject",),
    "rightsList": ("rights", "rightsUri", "rightsIdentifier"),
    "fundingReferences": ("funderName", "awardNumber", "awardTitle"),
    "relatedIdentifiers": ("relationType", "relatedIdentifier"),
}


def _is_record(item: object) -> bool:
    """A DataCite record carrying the fields `_normalize` reads, each of the type it reads."""
    if not isinstance(item, dict) or not isinstance(item.get("relationships"), dict | None):
        return False
    a = item.get("attributes")
    if not (
        isinstance(a, dict)
        and isinstance(a.get("doi"), str)
        and isinstance(a.get("types"), dict | None)
        and all(isinstance(a.get(k), str | None) for k in ("updated", "url"))
    ):
        return False
    for field, text_keys in _LIST_FIELDS.items():
        entries = a.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list) or not all(
            isinstance(e, dict) and all(isinstance(e.get(k), str | None) for k in text_keys)
            for e in entries
        ):
            return False
    return True


def _check_record(body: dict) -> None:
    if not _is_record(body.get("data")):
        raise _http.UpstreamEnvelopeError(f"no DataCite record in {body!r:.200}")


def _check_records(body: dict) -> None:
    data, meta = body.get("data"), body.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    if not (isinstance(data, list) and all(_is_record(d) for d in data) and type(total) is int):
        raise _http.UpstreamEnvelopeError(f"no DataCite record list in {body!r:.200}")


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
        status = _EU_REPO_ACCESS.get(str(r.get(field)).strip().lower())
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
        ident = str(r.get("rightsIdentifier")).lower()  # absent → "none": matches no rule
        uri = str(r.get("rightsUri")).lower()
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


def _creator_name(c: dict[str, Any]) -> str:
    """``name``, else "Family, Given" (DataCite's own form for ``name``) from the parts:
    90,315 records name a creator only by ``givenName``/``familyName`` (probe 2026-10-01)."""
    if c.get("name"):
        return c["name"]
    return ", ".join(p for p in (c.get("familyName"), c.get("givenName")) if p)


def _creator(c: dict[str, Any]) -> Creator | None:
    """The creator, or None when it has neither a name nor an ORCID."""
    orcid = None
    for nid in c.get("nameIdentifiers") or []:
        ident = str(nid.get("nameIdentifier"))  # absent → "None": not an ORCID
        scheme = str(nid.get("nameIdentifierScheme")).upper()
        # Only treat it as an ORCID when the source SAYS so — an ISNI/GND id can
        # match the ORCID shape, so the regex alone is not sufficient evidence.
        if scheme != "ORCID" and "orcid.org" not in ident.lower():
            continue
        cand = _orcid(ident)
        if cand:
            orcid = cand
            break
    name = _creator_name(c)
    return Creator(name=name, orcid=orcid) if name or orcid else None


def _normalize(item: dict[str, Any]) -> DataResource:
    a = item["attributes"]
    client_id = (((item.get("relationships") or {}).get("client") or {}).get("data") or {}).get(
        "id", ""
    )
    rt = (a.get("types") or {}).get("resourceTypeGeneral")
    rights = a.get("rightsList") or []
    license_ = _license_from_rights(rights)
    doi = a["doi"]
    return DataResource(
        id=f"datacite:{doi}",
        source=_source_for_client(client_id),
        kind=_KIND_MAP.get(str(rt), _pushdown.OTHER_KIND),  # absent → "None" → other
        title=_main(a.get("titles"), "title", "titleType", None) or "",
        creators=[c for c in map(_creator, a.get("creators") or []) if c is not None],
        funding=[
            FundingRef(funder=f["funderName"], award=f.get("awardNumber") or f.get("awardTitle"))
            for f in (a.get("fundingReferences") or [])
            if f.get("funderName")
        ],
        year=_year(a.get("publicationYear")),
        description=_main(a.get("descriptions"), "description", "descriptionType", "Abstract"),
        doi=doi,
        subjects=[s["subject"] for s in (a.get("subjects") or []) if s.get("subject")],
        license=license_,
        access=_access_from_rights(rights),
        metrics=_metrics(a),
        last_updated=a.get("updated"),
        links=[
            Link(rel=_rel(r["relationType"]), target_id=r["relatedIdentifier"])
            for r in (a.get("relatedIdentifiers") or [])
            if r.get("relationType") and r.get("relatedIdentifier")
        ],
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
        clauses.append(_pushdown.kind_clause(_KIND_FIELD, _KIND_MAP, kind))
    return [c for c in clauses if c is not None]


def pushable(filters: Mapping[str, Any], /) -> dict[str, Any]:
    """The active filters DataCite can evaluate: both year bounds, and any ``kind`` a
    ``resourceTypeGeneral`` normalizes to (``_pushdown.FilterPushdown``)."""
    act = _pushdown.active(filters)
    out = {k: v for k, v in act.items() if k in _pushdown.YEAR_FILTERS}
    kind = act.get("kind")
    if kind is not None and _filter_clauses({"kind": kind}):
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
    # With no sort (or an unknown one) DataCite orders by most recently updated, so a
    # search returned a feed of recent edits and page 2 shifted whenever a record was
    # touched between requests (probe 2026-09-29). Records with tied relevance scores can
    # still swap between requests; a second sort key ("relevance,name") is not accepted
    # and falls back to the update order.
    params = {"query": q, "sort": "relevance", "page[size]": str(capped)}
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page[number]"] = str(offset // capped + 1)
    body = await _http.request_json(
        client,
        _GET,
        f"{BASE_URL}/dois",
        service="DataCite search",
        params=params,
        headers=_ACCEPT_JSON,
        expect=dict,
        check=_check_records,
    )
    items = body["data"][offset % capped :]
    total = body["meta"]["total"]
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
            _GET,
            f"{BASE_URL}/dois/{_http.doi_path(doi)}",
            service="DataCite resolve",
            headers=_ACCEPT_JSON,
            expect=dict,
            check=_check_record,
        )
    except NotFoundError:
        raise NotFoundError(f"DataCite has no DOI {doi!r}") from None
    data = body["data"]
    resource = _normalize(data)
    record_doi = data["attributes"]["doi"]  # a str: _check_record requires it
    recid = _ZENODO_RECID_RE.search(record_doi) if resource.source == "zenodo" else None
    if recid is not None:
        return await zenodo.resolve(client, f"zenodo:{recid.group(1)}")
    resolver = _FILE_RESOLVERS.get(resource.source)
    if resolver is not None:
        if resource.source in _LANDING_AWARE:
            # The host repo is a federation (Dataverse): the record's own landing URL
            # names the installation that holds it. Any other server cannot resolve it.
            landing = data["attributes"].get("url")
            file_list = await resolver(client, record_doi, landing_url=landing)
        else:
            file_list = await resolver(client, record_doi)
        if file_list:
            resource = resource.model_copy(update={"files": file_list})
    return resource
