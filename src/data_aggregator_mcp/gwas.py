"""GWAS Catalog (EBI) — genome-wide association studies.

Discovery-only this wave: studies are keyed by curated disease-trait (case-
insensitive exact match on the GWAS trait vocabulary — NOT free text over
abstracts), and the value to the aggregator is the genotype<->phenotype study
metadata plus the PubMed cross-link (the paper<->data bridge). No fetch backend:
summary-statistics retrieval (FTP path derivation for fullPvalueSet studies) is a
documented follow-up, so gwas: ids are intentionally absent from
server._FETCHABLE_SOURCES and fail loud at fetch. kind="study".

REST API v2. The v1 API (``/gwas/rest/api/studies/search/findByDiseaseTrait``) was
kept alongside v2 only until May 2026 and now answers every request with HTTP 429.
A v2 study carries its PubMed id but not the paper: ``resolve`` reads the title and
date from ``/publications/{pmid}``; ``search`` does not spend a request per row on
it, so a search row is titled by its trait.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError, NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact, local_id

_API = "https://www.ebi.ac.uk/gwas/rest/api/v2"
SEARCH = f"{_API}/studies"
RECORD = f"{_API}/studies/{{acc}}"
PUBLICATION = f"{_API}/publications/{{pmid}}"
_LANDING = "https://www.ebi.ac.uk/gwas/studies/{acc}"
PREFIXES = {"gwas"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
# v2 is slow on every endpoint: 21-33 s per request, successful replies as late as
# 32.7 s, and about 1 in 4 an HTTP 500 at ~30.5 s (probes 2026-09-28 20:15 and
# 21:50 EDT). A 30 s timeout cut the late replies on every retry alike, so the timeout
# sits above the slowest reply seen; retries are for the 500s.
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3


def _normalize(s: dict, pub: dict | None = None) -> DataResource:
    acc = s.get("accession_id") or ""
    pub = pub or {}
    pubmed = s.get("pubmed_id")
    identifiers: dict[str, str] = {}
    if pubmed:
        identifiers["pmid"] = str(pubmed)
    trait = s.get("disease_trait") or None
    pubdate = pub.get("publication_date") or ""
    year = int(pubdate[:4]) if pubdate[:4].isdigit() else None
    return DataResource(
        id=f"gwas:{acc}",
        source="gwas",
        kind="study",
        title=pub.get("title") or trait or acc,
        year=year,
        identifiers=identifiers,
        subjects=[trait] if trait else [],
        last_updated=pub.get("publication_date") or None,
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
        # disease_trait is the v2 form of v1's findByDiseaseTrait: case-insensitive exact
        # match on the trait (efo_trait is a broader ontology match; not used).
        params={"disease_trait": query, "size": capped, "page": page},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        # No not_found_returns: a 404 on the SEARCH endpoint means it moved (an outage),
        # not "no studies" — an empty result is a 200 with an empty `studies` list.
        expect=dict,
    )
    studies = ((body or {}).get("_embedded") or {}).get("studies") or []
    # `.get(k, default)` only falls back on an ABSENT key, not an explicit null —
    # coerce a None totalElements to the page length so total stays an int.
    reported = ((body or {}).get("page") or {}).get("totalElements")
    total = reported if isinstance(reported, int) else len(studies)
    # Page-boundary slice (see pagination spec): the page holding `offset` starts at
    # `page * capped`, so drop the first `offset % capped` rows — otherwise a mid-page
    # offset replays rows the router already consumed.
    sliced = studies[offset % capped :] if capped else studies
    return total, [compact(_normalize(s)) for s in sliced]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = local_id(resource_id, "gwas", strip=True)
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
    if not body or not body.get("accession_id"):
        raise NotFoundError(f"GWAS Catalog has no study {acc}")
    pub, pub_error = await _publication(client, body.get("pubmed_id"))
    resource = _normalize(body, pub)
    if pub_error:  # recorded, so the router does not cache a study missing its paper
        resource = resource.model_copy(
            update={"errors": {**resource.errors, "publication": pub_error}}
        )
    return resource


async def _publication(client: httpx.AsyncClient, pmid: object) -> tuple[dict, str | None]:
    """The study's paper (title, publication_date), and why it is missing (None when the
    Catalog answered). No PMID, or a PMID the Catalog has no publication for → ``({},
    None)``; a failed lookup → ``({}, reason)``."""
    if not pmid:
        return {}, None
    try:
        body = await _http.request_json(
            client,
            "GET",
            PUBLICATION.format(pmid=pmid),
            service="GWAS Catalog publication",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            not_found_returns=None,
            expect=dict,  # a wrong-shape body is a failed lookup, not "no publication"
        )
    except DataAggregatorError as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return body or {}, None
