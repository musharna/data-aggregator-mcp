"""DataCite archives discovery adapter — async wrapper around api.datacite.org.

Discovery: GET /dois?query=...  (one query spans every DataCite client —
Dryad, Zenodo, Figshare, Dataverse, OSF, Mendeley, ...).
Resolve:   GET /dois/{doi}

DataCite metadata itself carries no file manifest, so ``_normalize`` returns
files=[]. ``resolve`` then attaches files[] by dispatching on the detected
``source`` to each host repo's native API via ``_FILE_RESOLVERS`` (Figshare,
Dataverse, OSF — fetchable; Dryad — manifest-only). A Zenodo DOI is delegated to
``zenodo.resolve`` so its files[] populate from the native adapter; unrecognized
repos stay files=[]. Fetchability is enforced post-resolve by the server fetch guard.
"""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http, dataverse, dryad, figshare, osf, zenodo
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FundingRef,
    Link,
    _orcid,
    _rel,
    compact,
)

BASE_URL = "https://api.datacite.org"
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
)


def _source_for_client(client_id: str) -> str:
    cid = client_id.lower()
    for needle, name in _SOURCE_RULES:
        if needle in cid:
            return name
    return client_id


# source name → per-repo file-manifest resolver (populates files[] at resolve).
# Dryad is included (manifest-only); fetchability is gated separately in server.py.
_FILE_RESOLVERS = {
    "dryad": dryad.files,
    "figshare": figshare.files,
    "dataverse": dataverse.files,
    "osf": osf.files,
}


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


def _access_from_rights(rights_list: list[dict[str, Any]] | None) -> str | None:
    """DataCite has no access field; infer 'open' from an open-content license
    (Creative Commons / public domain). Otherwise None — do not guess."""
    for r in rights_list or []:
        ident = (r.get("rightsIdentifier") or "").lower()
        uri = (r.get("rightsUri") or "").lower()
        if ident.startswith("cc") or "creativecommons.org" in uri or "publicdomain" in uri:
            return "open"
    return None


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
    license_ = None
    if rights:
        license_ = rights[0].get("rightsIdentifier") or rights[0].get("rights")
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
        links=[
            Link(rel=_rel(r["relationType"]), target_id=r["relatedIdentifier"])
            for r in (a.get("relatedIdentifiers") or [])
            if r.get("relationType") and r.get("relatedIdentifier")
        ],
        files=[],  # DataCite is metadata-only
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Search DataCite DOIs. Returns (total_hits, COMPACT resources).
    ``offset`` → page ``offset // size + 1`` then drop first ``offset % size``."""
    capped = min(size, MAX_SIZE)
    params = {"query": query, "page[size]": str(capped)}
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
    doi = resource_id.split(":", 1)[1] if resource_id.startswith("datacite:") else resource_id
    try:
        body = await _http.request_json(
            client,
            "GET",
            f"{BASE_URL}/dois/{doi}",
            service="DataCite resolve",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"DataCite has no DOI {doi!r}") from None
    resource = _normalize(body["data"])
    if resource.source == "zenodo" and resource.doi and "zenodo." in resource.doi:
        recid = resource.doi.rsplit("zenodo.", 1)[-1]
        if recid.isdigit():
            return await zenodo.resolve(client, f"zenodo:{recid}")
    resolver = _FILE_RESOLVERS.get(resource.source)
    if resolver is not None and resource.doi:
        file_list = await resolver(client, resource.doi)
        if file_list:
            resource = resource.model_copy(update={"files": file_list})
    return resource
