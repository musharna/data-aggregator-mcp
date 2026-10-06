"""OpenAIRE Graph API literature backend.

Discovery via the Graph API ``researchProducts`` endpoint (type=publication).
Resolve re-fetches the single entity and queries the ScholeXplorer Scholix API
(via ``scholix``) by the publication's DOI for data links, and adds the accessions
Europe PMC mines from its text (``europepmc``) when idconv finds its PMID. Paper→dataset link
yield is best-effort: most OpenAIRE link edges from a paper are citations, which
``scholix`` drops; the primary value here is broad publication discovery.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping

import httpx

from data_aggregator_mcp import _http, europepmc, fulltext, idconv, omics, scholix
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, normalize_access

DEFAULT_SIZE = 10
MAX_SIZE = 50
BASE_URL = "https://api.openaire.eu/graph/v1/researchProducts"
# The search answers HTTP 400 "Too many logical operators found. Max allowed is 4" past
# four upper-case AND/OR/NOT words outside quotes (probed live 2026-10-04). The router
# sends this backend a shortened ontology expansion that fits (_ontology.within_operator_limit).
MAX_OPERATORS = 4
_GET = "GET"
_ESCAPE_ALL = ""  # quote(safe=...): an id is one path segment, so even "/" is escaped

_TAG = re.compile(r"<[^>]+>")  # bounded, no nested quantifiers — safe on short strings
_WS = re.compile(r"\s+")

# The Graph API's four research-product types, each also a DataResource kind. Search
# asks for publications only, but the entity endpoint serves every type.
_KINDS = frozenset({"publication", "dataset", "software", "other"})

# Every field ``_normalize_openaire`` reads, at the type it reads it as. Each may be
# absent or null (OpenAIRE writes an empty list as null), except the record's ``id``.
_RECORD_FIELDS = {
    "mainTitle": str,
    "publicationDate": str,
    "type": str,
    "authors": list,
    "descriptions": list,
    "pids": list,
    "instances": list,
    "subjects": list,
    "bestAccessRight": dict,
}
_AUTHOR_FIELDS = {"fullName": str}
_PID_FIELDS = {"scheme": str, "value": str}
_INSTANCE_FIELDS = {"pids": list, "license": str}
_SUBJECT_FIELDS = {"subject": dict}
_VALUE_FIELDS = {"value": str}
_ACCESS_FIELDS = {"label": str}


def _strip_tags(text: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", text)).strip()


def _typed(item: object, kinds: Mapping[str, type]) -> bool:
    return isinstance(item, dict) and all(
        item.get(k) is None or isinstance(item[k], t) for k, t in kinds.items()
    )


def _pids_typed(pids: list | None) -> bool:
    return all(_typed(p, _PID_FIELDS) for p in pids or [])


def _is_record(record: object) -> bool:
    """A non-empty ``id``, and every field ``_normalize_openaire`` reads at its type."""
    if not (isinstance(record, dict) and _typed(record, _RECORD_FIELDS)):
        return False
    rid = record.get("id")
    return (
        isinstance(rid, str)
        and rid != ""
        and all(_typed(a, _AUTHOR_FIELDS) for a in record.get("authors") or [])
        and all(d is None or isinstance(d, str) for d in record.get("descriptions") or [])
        and _pids_typed(record.get("pids"))
        and all(
            _typed(i, _INSTANCE_FIELDS) and _pids_typed(i.get("pids"))
            for i in record.get("instances") or []
        )
        and all(
            _typed(s, _SUBJECT_FIELDS) and _typed(s.get("subject") or {}, _VALUE_FIELDS)
            for s in record.get("subjects") or []
        )
        and _typed(record.get("bestAccessRight") or {}, _ACCESS_FIELDS)
    )


def _check_record(body: dict) -> None:
    if not _is_record(body):
        raise _http.UpstreamEnvelopeError(f"no OpenAIRE record in {body!r:.200}")


def _check_page(body: dict) -> None:
    """The hit count and result list every search answer carries: no match is
    ``numFound`` 0 and ``results: []``, and a page past the last is ``results: []``."""
    header, results = body.get("header"), body.get("results")
    if not (
        isinstance(header, dict)
        and type(header.get("numFound")) is int
        and isinstance(results, list)
        and all(_is_record(r) for r in results)
    ):
        raise _http.UpstreamEnvelopeError(f"no OpenAIRE result list in {body!r:.200}")


def _doi_of(record: dict) -> str | None:
    def _scan(pids: list[dict] | None) -> str | None:
        for p in pids or []:
            scheme = p.get("scheme")
            if isinstance(scheme, str) and scheme.lower() == "doi" and p.get("value"):
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
        lic = (inst.get("license") or "").strip()
        if lic:
            return lic
    return None


def _kind_of(record: dict) -> str:
    kind = record.get("type")
    return kind if kind in _KINDS else "other"


def _normalize_openaire(record: dict) -> DataResource:
    descriptions = record.get("descriptions") or []
    description = _strip_tags(descriptions[0]) if descriptions and descriptions[0] else None
    return DataResource(
        id=f"openaire:{record['id']}",
        source="openaire",
        kind=_kind_of(record),
        title=record.get("mainTitle") or "",
        creators=[
            Creator(name=a["fullName"]) for a in (record.get("authors") or []) if a.get("fullName")
        ],
        year=omics._year_from(record.get("publicationDate")),
        description=description,
        doi=_doi_of(record),
        subjects=[
            s["subject"]["value"]
            for s in (record.get("subjects") or [])
            if (s.get("subject") or {}).get("value")
        ],
        license=_license_of(record),
        access=_access_of(record),
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
        _GET,
        BASE_URL,
        service="OpenAIRE",
        params=params,
        expect=dict,
        check=_check_page,
    )
    results = data["results"][offset % capped :]
    return data["header"]["numFound"], [_normalize_openaire(r) for r in results]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``openaire:<internal id>`` to a full record + Scholix data links."""
    prefix, _, oid = resource_id.partition(":")
    if prefix != "openaire" or not oid:
        raise NotFoundError(f"unroutable openaire id {resource_id!r}")
    record = await _http.request_json(
        client,
        _GET,
        f"{BASE_URL}/{urllib.parse.quote(oid, safe=_ESCAPE_ALL)}",
        service="OpenAIRE",
        expect=dict,
        check=_check_record,
    )
    resource = _normalize_openaire(record)
    links, links_error = await scholix.links_for(client, resource.doi)
    ids, ids_error = await idconv.identifiers_for(client, resource.doi)
    mined, mined_cut, mined_error = await europepmc.mined_links(
        client, pmid=ids.get("pmid"), pmcid=ids.get("pmcid")
    )
    links = europepmc.merge(links, mined)
    ft = await fulltext.find(client, pmcid=ids.get("pmcid"), doi=resource.doi)
    update: dict = {}
    # Each failed lookup is recorded, so the router does not cache a degraded record.
    errors = {**resource.errors}
    if ids:
        update["identifiers"] = ids
    if links:
        update["links"] = links
    if mined_cut:
        update["truncated"] = {"links": mined_cut}
    links_errors = "; ".join(e for e in (links_error, mined_error) if e)
    if links_errors:
        errors["links"] = links_errors
    if ids_error:
        errors["identifiers"] = ids_error
    if ft.error:
        errors["files"] = ft.error
    if errors != resource.errors:
        update["errors"] = errors
    if ft.file is not None:
        update["files"] = [ft.file]
    if resource.access is None and ft.access:
        update["access"] = ft.access
    if resource.license is None and ft.license:
        update["license"] = ft.license
    if update:
        resource = resource.model_copy(update=update)
    return resource
