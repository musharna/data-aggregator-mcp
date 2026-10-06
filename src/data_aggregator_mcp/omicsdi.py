"""OmicsDI (Omics Discovery Index) — proteomics/metabolomics discovery.

Restricted to the mass-spec modality repos OmicsDI uniquely adds; GEO /
ArrayExpress / ENA hits are left out (already covered by the omics leg, and
accession-keyed so the DOI dedup would miss the duplicates). Resolve (Task 6)
routes fetchable files to PRIDE / MetaboLights; other repos are discovery-only.

The restriction is sent upstream as a ``repository:`` clause, so OmicsDI's ``count``
and ``start`` are in the same coordinates as the records returned and search pages.
It used to filter each page after fetch, which made it page-1-only and could empty a
page: "Chlamydomonas nitrogen" returned 3 hits of the 36 mass-spec datasets OmicsDI
holds, none of them jPOST (2026-10-05 head-to-head).
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http, metabolights, pride
from data_aggregator_mcp._relevance import with_plurals
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, DataResource, Link, compact

SEARCH = "https://www.omicsdi.org/ws/dataset/search"
RECORD = "https://www.omicsdi.org/ws/dataset/{source}/{acc}"
_LANDING = "https://www.omicsdi.org/dataset/{source}/{acc}"
PREFIXES = {"omicsdi"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
# Matches words exactly, so search sends each word with its plural (``plurals``): "coral reef" 89
# hits, "coral reefs" 37, "coral (reef OR reefs)" 98 (probed 2026-10-05).
QUERY_PLURALS = True
# OmicsDI matches only records holding every word of the query ("Chlamydomonas
# nitrogen" 255, "+ starvation" 71, "+ phosphoproteomics" 4; probed 2026-10-05), so the
# router names this source when a multi-word search comes back empty.
REQUIRES_EVERY_WORD = True
MAX_RETRIES = 2
_ACCEPT_JSON = {"Accept": "application/json"}
# A module constant: mutmut does not mutate those, and a lower-cased method is the same
# request (httpx upper-cases it); test_omicsdi_observed pins the method sent.
_GET = "GET"

# OmicsDI `repository` names (its search facet) for the mass-spectrometry modality we
# uniquely add (proteomics + metabolomics). Deliberately excludes EGA (controlled-access
# human genomics, not MS) and the transcriptomics repos (GEO/ArrayExpress/ENA) the omics
# leg already covers. jPOST, iProX and Panorama Public are ProteomeXchange members.
_MODALITY_REPOSITORIES = (
    "pride",
    "MassIVE",
    "jPOST",
    "iProX",
    "PeptideAtlas",
    "PanoramaPublic",
    "MetaboLights",
    "MetabolomicsWorkbench",
    "GNPS",
)
_MODALITY_CLAUSE = "repository:(" + " OR ".join(f'"{r}"' for r in _MODALITY_REPOSITORIES) + ")"


def _modality_query(query: str) -> str:
    """``query`` restricted upstream to the mass-spec repositories."""
    return f"({query}) AND {_MODALITY_CLAUSE}" if query.strip() else _MODALITY_CLAUSE


# A source code or an accession, the two parts of `omicsdi:<source>:<acc>`. Both go
# into the record URL's path, so a `/`, `..`, `?`, `#` or `%` must never reach it;
# every live search hit sampled (1,561 of 1,561, 2026-10-02) matches.
_ID_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# OmicsDI `additional` is a free-form, repo-specific dict of string lists; the same
# concept hides under different keys per source repo (PRIDE uses `submitter`/`species`,
# MetaboLights uses `submitter_name`/`organism`). These helpers read the first present
# key and never fabricate — a missing key yields nothing. `creators` = the dataset
# DEPOSITOR (submitter); we deliberately do NOT fall back to publication `author`, which
# is paper authorship, not dataset creation.
_CREATOR_KEYS = ("submitter", "submitter_name")
_ORGANISM_KEYS = ("species", "organism")
# Every `additional` key the reader reads; `_check_record` holds each to a string list.
_READ_KEYS = (*_CREATOR_KEYS, *_ORGANISM_KEYS, "publication", "doi")
# `publication` is free text: PRIDE writes "<pmid> <citation> <doi>", MetaboLights
# "<title>. <doi>. PMID:<pmid>". A DOI may sit anywhere; trailing punctuation is the
# sentence's, not the DOI's.
_DOI_RE = re.compile(r"\b10\.[0-9]{4,}/[^\s\"<>]+")
_DOI_TRAILING = ".,;:"
_LEADING_PMID_RE = re.compile(r"\s*([0-9]+)(?!\S)")
_PMID_LABEL_RE = re.compile(r"\bPMID:\s*([0-9]+)")


def _opt(value: object, typ: type) -> bool:
    return value is None or isinstance(value, typ)


def _is_str_list(value: object) -> bool:
    return value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value))


def _is_hit(d: object) -> bool:
    """A search hit carrying every field ``search`` reads, at the type it reads it as."""
    return (
        isinstance(d, dict)
        and isinstance(d.get("source"), str)
        and isinstance(d.get("id"), str)
        and _opt(d.get("title"), str)
        and _opt(d.get("description"), str)
    )


def _check_search(body: dict) -> None:
    datasets = body.get("datasets")
    if not (isinstance(datasets, list) and all(_is_hit(d) for d in datasets)):
        raise _http.UpstreamEnvelopeError(f"no OmicsDI dataset list in {body!r:.200}")
    count = body.get("count")
    # bool is an int subclass; a count below the page it came with is not a count.
    if type(count) is not int or count < len(datasets):
        raise _http.UpstreamEnvelopeError(f"no OmicsDI result count in {body!r:.200}")


def _check_record(body: dict, acc: str) -> None:
    """The record for ``acc`` (OmicsDI accessions are case-sensitive), with every
    field ``_record`` reads at the type it reads it as (absent or null is fine)."""
    additional = body.get("additional")
    if not (
        body.get("accession") == acc
        and _opt(body.get("name"), str)
        and _opt(body.get("description"), str)
        and _opt(additional, dict)
        and all(_is_str_list((additional or {}).get(key)) for key in _READ_KEYS)
    ):
        raise _http.UpstreamEnvelopeError(f"no OmicsDI record {acc} in {body!r:.200}")


def _str_list(additional: dict, keys: tuple[str, ...]) -> list[str]:
    """The first key whose list has a non-blank string, stripped and deduped in order.
    Never crosses repos to merge keys."""
    for key in keys:
        seen = dict.fromkeys(v.strip() for v in additional.get(key) or [] if v.strip())
        if seen:
            return list(seen)
    return []


def _publications(entries: list[str]) -> tuple[str | None, list[str]]:
    """The first PMID (a leading all-digit token, or a ``PMID:`` label) and every
    distinct paper DOI in the free-text ``additional.publication`` strings."""
    pmid = None
    dois: dict[str, None] = {}
    for entry in entries:
        m = _LEADING_PMID_RE.match(entry) or _PMID_LABEL_RE.search(entry)
        if pmid is None and m:
            pmid = m.group(1)
        for found in _DOI_RE.findall(entry):
            dois.setdefault(found.rstrip(_DOI_TRAILING))
    return pmid, list(dois)


def _own_doi(additional: dict) -> str | None:
    """The dataset's own DOI (PRIDE lists it under ``additional.doi``), never a paper's."""
    return next((v for v in additional.get("doi") or [] if _DOI_RE.fullmatch(v)), None)


def _normalize(d: dict) -> DataResource:
    source, acc = d["source"], d["id"]
    return DataResource(
        id=f"omicsdi:{source}:{acc}",
        source="omicsdi",
        kind="study",
        title=d.get("title") or "",
        description=d.get("description"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))],
    )


def _record(resource_id: str, source: str, acc: str, body: dict) -> DataResource:
    additional = body.get("additional") or {}
    pmid, paper_dois = _publications(additional.get("publication") or [])
    links = [Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))]
    links += [Link(rel="described_in", target_id=doi) for doi in paper_dois]
    return DataResource(
        id=resource_id,
        source="omicsdi",
        kind="study",
        title=body.get("name") or "",
        description=body.get("description"),
        creators=[Creator(name=n) for n in _str_list(additional, _CREATOR_KEYS)],
        organism=_str_list(additional, _ORGANISM_KEYS),
        doi=_own_doi(additional),
        identifiers={"pmid": pmid} if pmid else {},
        links=links,
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    plurals: bool = True,
) -> tuple[int, list[DataResource]]:
    words = with_plurals(query) if plurals else query
    params = {"query": _modality_query(words), "size": str(min(size, MAX_SIZE))}
    if offset:
        params["start"] = str(offset)
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service="OmicsDI search",
        params=params,
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        expect=dict,
        check=_check_search,
    )
    return body["count"], [compact(_normalize(d)) for d in body["datasets"]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    parts = resource_id.split(":")  # omicsdi:<source>:<acc>
    if len(parts) != 3 or not all(_ID_PART_RE.fullmatch(p) for p in parts[1:]):
        raise NotFoundError(f"malformed OmicsDI id {resource_id!r}")
    _prefix, source, acc = parts
    body = await _http.request_json(
        client,
        _GET,
        RECORD.format(source=source, acc=acc),
        service="OmicsDI resolve",
        headers=_ACCEPT_JSON,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
        expect=dict,
        check=lambda b: _check_record(b, acc),
    )
    if body is None:
        raise NotFoundError(f"OmicsDI has no {source}/{acc}")
    resource = _record(resource_id, source, acc, body)
    if source == "pride":
        file_list = await pride.files(client, acc)
    elif source == "metabolights_dataset":
        file_list = await metabolights.files(client, acc)
    else:
        file_list = []
    return resource.model_copy(update={"files": file_list})
