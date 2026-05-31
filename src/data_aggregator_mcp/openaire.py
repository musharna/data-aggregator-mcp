"""OpenAIRE Graph API literature backend.

Discovery via the Graph API ``researchProducts`` endpoint (type=publication).
Resolve re-fetches the single entity and queries the ScholeXplorer Scholix API
(via ``scholix``) by the publication's DOI for data links. Paper→dataset link
yield is best-effort: most OpenAIRE link edges from a paper are citations, which
``scholix`` drops; the primary value here is broad publication discovery.
"""

from __future__ import annotations

import re
import urllib.parse

import httpx

from data_aggregator_mcp import _http, fulltext, idconv, omics, scholix
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, normalize_access

DEFAULT_SIZE = 10
MAX_SIZE = 50
BASE_URL = "https://api.openaire.eu/graph/v1/researchProducts"

_TAG = re.compile(r"<[^>]+>")  # bounded, no nested quantifiers — safe on short strings
_WS = re.compile(r"\s+")


def _strip_tags(text: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


def _doi_of(record: dict) -> str | None:
    def _scan(pids: list[dict] | None) -> str | None:
        for p in pids or []:
            if (p.get("scheme") or "").lower() == "doi" and p.get("value"):
                return p["value"]
        return None

    return _scan(record.get("pids")) or next(
        (d for inst in (record.get("instances") or []) if (d := _scan(inst.get("pids")))),
        None,
    )


def _access_of(record: dict) -> str | None:
    label = ((record.get("bestAccessRight") or {}).get("label")) or None
    return normalize_access(label)


def _license_of(record: dict) -> str | None:
    for inst in record.get("instances") or []:
        lic = inst.get("license")
        if lic and str(lic).strip():
            return str(lic).strip()
    return None


def _normalize_openaire(record: dict) -> DataResource:
    descriptions = record.get("descriptions") or []
    description = _strip_tags(descriptions[0]) if descriptions and descriptions[0] else None
    return DataResource(
        id=f"openaire:{record.get('id', '')}",
        source="openaire",
        kind="publication",
        title=record.get("mainTitle") or "",
        creators=[a["fullName"] for a in (record.get("authors") or []) if a.get("fullName")],
        year=omics._year_from(record.get("publicationDate")),
        description=description,
        doi=_doi_of(record),
        subjects=[
            s["subject"]["value"]
            for s in (record.get("subjects") or [])
            if s.get("subject", {}).get("value")
        ],
        license=_license_of(record),
        access=_access_of(record),
        files=[],
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    params: dict[str, str | int] = {"search": query, "type": "publication", "pageSize": capped}
    if offset:  # only when paging past page 1, so offset=0 request stays byte-identical
        params["page"] = offset // capped + 1
    data = await _http.request_json(
        client,
        "GET",
        BASE_URL,
        service="OpenAIRE",
        params=params,
    )
    total = int(data.get("header", {}).get("numFound", 0) or 0)
    results = (data.get("results", []) or [])[offset % capped :]
    return total, [_normalize_openaire(r) for r in results]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``openaire:<internal id>`` to a full record + Scholix data links."""
    prefix, _, oid = resource_id.partition(":")
    if prefix != "openaire" or not oid:
        raise NotFoundError(f"unroutable openaire id {resource_id!r}")
    record = await _http.request_json(
        client,
        "GET",
        f"{BASE_URL}/{urllib.parse.quote(oid, safe='')}",
        service="OpenAIRE",
    )
    record["id"] = record.get("id") or oid  # single-entity payload may omit/null its own id
    resource = _normalize_openaire(record)
    links = await scholix.links_for(client, resource.doi)
    ids = await idconv.identifiers_for(client, resource.doi)
    ft = await fulltext.find(client, pmcid=ids.get("pmcid"), doi=resource.doi)
    update: dict = {}
    if ids:
        update["identifiers"] = ids
    if links:
        update["links"] = links
    if ft.file is not None:
        update["files"] = [ft.file]
    if resource.access is None and ft.access:
        update["access"] = ft.access
    if resource.license is None and ft.license:
        update["license"] = ft.license
    if update:
        resource = resource.model_copy(update=update)
    return resource
