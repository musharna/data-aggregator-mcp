"""GWAS Catalog (EBI) — genome-wide association studies.

Discovery-only this wave: studies are keyed by curated disease-trait (case-
insensitive exact match on the GWAS trait vocabulary — NOT free text over
abstracts), and the value to the aggregator is the genotype<->phenotype study
metadata plus the PubMed cross-link (the paper<->data bridge). No fetch backend:
summary-statistics retrieval (FTP path derivation for fullPvalueSet studies) is a
documented follow-up, so gwas: ids are intentionally absent from
server._FETCHABLE_SOURCES and fail loud at fetch. kind="study".
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact

SEARCH = "https://www.ebi.ac.uk/gwas/rest/api/studies/search/findByDiseaseTrait"
RECORD = "https://www.ebi.ac.uk/gwas/rest/api/studies/{acc}"
_LANDING = "https://www.ebi.ac.uk/gwas/studies/{acc}"
PREFIXES = {"gwas"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _normalize(s: dict) -> DataResource:
    acc = s.get("accessionId") or ""
    pub = s.get("publicationInfo") or {}
    pubmed = pub.get("pubmedId")
    identifiers: dict[str, str] = {}
    if pubmed:
        identifiers["pmid"] = str(pubmed)
    trait = (s.get("diseaseTrait") or {}).get("trait")
    pubdate = pub.get("publicationDate") or ""
    year = int(pubdate[:4]) if pubdate[:4].isdigit() else None
    return DataResource(
        id=f"gwas:{acc}",
        source="gwas",
        kind="study",
        title=pub.get("title") or trait or acc,
        year=year,
        identifiers=identifiers,
        subjects=[trait] if trait else [],
        last_updated=pub.get("publicationDate") or None,
        files=[],
        links=[Link(rel="landing_page", target_id=_LANDING.format(acc=acc))],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    page = offset // capped if capped else 0
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="GWAS Catalog search",
        params={"diseaseTrait": query, "size": capped, "page": page},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns={},
    )
    studies = ((body or {}).get("_embedded") or {}).get("studies") or []
    # `.get(k, default)` only falls back on an ABSENT key, not an explicit null —
    # coerce a None totalElements to the page length so total stays an int.
    reported = ((body or {}).get("page") or {}).get("totalElements")
    total = reported if isinstance(reported, int) else len(studies)
    return total, [compact(_normalize(s)) for s in studies]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not acc:
        raise NotFoundError(f"malformed GWAS id {resource_id!r}")
    body = await _http.request_json(
        client,
        "GET",
        RECORD.format(acc=acc),
        service="GWAS Catalog resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if not body or not body.get("accessionId"):
        raise NotFoundError(f"GWAS Catalog has no study {acc}")
    return _normalize(body)
