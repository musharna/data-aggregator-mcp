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

import functools
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError, NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact, local_id, year_from

_API = "https://www.ebi.ac.uk/gwas/rest/api/v2"
SEARCH = f"{_API}/studies"
RECORD = f"{_API}/studies/{{acc}}"
PUBLICATION = f"{_API}/publications/{{pmid}}"
_LANDING = "https://www.ebi.ac.uk/gwas/studies/{acc}"
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
PREFIXES = {"gwas"}
# A study accession is GCST + digits; it goes into the URL path.
_ACC_RE = re.compile(r"GCST[0-9]+", re.IGNORECASE)
DEFAULT_SIZE = 10
MAX_SIZE = 50
# v2 is slow on every endpoint: 21-33 s per request, successful replies as late as
# 32.7 s, and about 1 in 4 an HTTP 500 at ~30.5 s (probes 2026-09-28 20:15 and
# 21:50 EDT). A 30 s timeout cut the late replies on every retry alike, so the timeout
# sits above the slowest reply seen; retries are for the 500s.
DEFAULT_TIMEOUT = 60.0


def _is_study(s: object) -> bool:
    """A study accession, and every other field ``_normalize`` reads at the type it
    reads it as (absent or null is fine). The PubMed id is an int: it goes into the
    publication URL's path."""
    if not isinstance(s, dict):
        return False
    acc, pmid, trait = s.get("accession_id"), s.get("pubmed_id"), s.get("disease_trait")
    return (
        isinstance(acc, str)
        and _ACC_RE.fullmatch(acc) is not None
        and (pmid is None or type(pmid) is int)
        and (trait is None or isinstance(trait, str))
    )


def _check_study(body: dict) -> None:
    if not _is_study(body):
        raise _http.UpstreamEnvelopeError(f"no GWAS Catalog study in {body!r:.200}")


def _check_page(body: dict, *, first: int) -> None:
    """An int ``page.totalElements`` and a list of studies. The Catalog leaves out
    ``_embedded`` on a page that holds no rows (no hits, or a page past the end), so
    it may be absent only when the total does not reach ``first``, the page's first
    row."""
    page = body.get("page")
    total = page.get("totalElements") if isinstance(page, dict) else None
    if "_embedded" in body:
        embedded = body["_embedded"]
        studies = embedded.get("studies") if isinstance(embedded, dict) else None
    else:
        studies = [] if type(total) is int and total <= first else None
    if not (
        type(total) is int and isinstance(studies, list) and all(_is_study(s) for s in studies)
    ):
        raise _http.UpstreamEnvelopeError(f"no GWAS Catalog study list in {body!r:.200}")


def _check_publication(body: dict) -> None:
    if not all(isinstance(body.get(k), str | None) for k in ("title", "publication_date")):
        raise _http.UpstreamEnvelopeError(f"no GWAS Catalog publication in {body!r:.200}")


def _normalize(s: dict, pub: dict) -> DataResource:
    acc = s["accession_id"]
    pubmed = s.get("pubmed_id")
    trait = s.get("disease_trait")
    pubdate = pub.get("publication_date")
    return DataResource(
        id=f"gwas:{acc}",
        source="gwas",
        kind="study",
        title=pub.get("title") or trait or acc,
        year=year_from(pubdate),
        identifiers={"pmid": str(pubmed)} if pubmed else {},
        subjects=[trait] if trait else [],
        last_updated=pubdate or None,
        links=[Link(rel="landing_page", target_id=_LANDING.format(acc=acc))],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    page = offset // capped  # size >= 1: the search tool's schema refuses less
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service="GWAS Catalog search",
        # disease_trait is the v2 form of v1's findByDiseaseTrait: case-insensitive exact
        # match on the trait (efo_trait is a broader ontology match; not used).
        params={"disease_trait": query, "size": capped, "page": page},
        headers=_ACCEPT_JSON,
        timeout=DEFAULT_TIMEOUT,
        # No not_found_returns: a 404 on the SEARCH endpoint means it moved (an outage),
        # not "no studies" — an empty result is a 200 with no `_embedded` and a total.
        expect=dict,
        check=functools.partial(_check_page, first=page * capped),
    )
    studies = body["_embedded"]["studies"] if "_embedded" in body else []
    # Page-boundary slice (see pagination spec): the page holding `offset` starts at
    # `page * capped`, so drop the first `offset % capped` rows — otherwise a mid-page
    # offset replays rows the router already consumed.
    sliced = studies[offset % capped :]
    return body["page"]["totalElements"], [compact(_normalize(s, {})) for s in sliced]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    acc = local_id(resource_id, "gwas", strip=True, upper=True)
    if not _ACC_RE.fullmatch(acc):
        raise NotFoundError(f"malformed GWAS id {resource_id!r}")
    body = await _http.request_json(
        client,
        _GET,
        RECORD.format(acc=acc),
        service="GWAS Catalog resolve",
        headers=_ACCEPT_JSON,
        timeout=DEFAULT_TIMEOUT,
        not_found_returns=None,
        expect=dict,
        check=_check_study,
    )
    if body is None:
        raise NotFoundError(f"GWAS Catalog has no study {acc}")
    pub, pub_error = await _publication(client, body.get("pubmed_id"))
    resource = _normalize(body, pub)
    if pub_error:  # recorded, so the router does not cache a study missing its paper
        resource = resource.model_copy(update={"errors": {"publication": pub_error}})
    return resource


async def _publication(client: httpx.AsyncClient, pmid: int | None) -> tuple[dict, str | None]:
    """The study's paper (title, publication_date), and why it is missing (None when the
    Catalog answered). No PMID, or a PMID the Catalog has no publication for → ``({},
    None)``; a failed lookup → ``({}, reason)``."""
    if not pmid:
        return {}, None
    try:
        body = await _http.request_json(
            client,
            _GET,
            PUBLICATION.format(pmid=pmid),
            service="GWAS Catalog publication",
            headers=_ACCEPT_JSON,
            timeout=DEFAULT_TIMEOUT,
            not_found_returns=None,
            # a wrong-shape body is a failed lookup, not "no publication"
            expect=dict,
            check=_check_publication,
        )
    except DataAggregatorError as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return body or {}, None
