"""Trust / integrity signals — retraction status via Crossref.

resolve(trust=True) calls annotate() to attach a TrustSignals to a resolved
resource. The retraction check is ONE Crossref /works/{doi} call: a retracted
work carries its retraction under message.updated-by[] (verified live on real
retracted DOIs 2026-06-10 — NOT update-to[], which is the retraction notice's
inverse view). A notice that takes all or part of the work out of the record —
a retraction, partial retraction, withdrawal or removal (``_RETRACTIONS``) — reads
as retracted; an expression of concern reads as concern. A DOI Crossref doesn't
register (e.g. a DataCite data DOI) 404s → all fields stay None (unknown, NOT a
false "clean" claim). A 200 that is not a work with a well-formed updated-by list
is a failed lookup, never a clean one.

The checked DOIs are the record's own DOI AND every ``described_in`` paper DOI in
links[] (a PDB entry's primary citation, a BioStudies study's publication): a record
built on a retracted paper is flagged even when its own data DOI is clean. The
verdicts combine as: any retraction → retracted; else any failed lookup → unknown
(never a false "clean"); else any found work → False; else unknown. This is
enrichment: any failure (Crossref outage, timeout, parse error) logs a warning and
returns all-None (= unknown — the honest state when we couldn't check) — it never
raises into an otherwise-valid resolve (spec §8). So resolve(trust=True) is never
less reliable than resolve without it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource, TrustSignals

logger = logging.getLogger(__name__)

CROSSREF = "https://api.crossref.org/works/{doi}"
# Crossref update types whose notice takes all or part of a work out of the record
# (live facet 2026-10-02: retraction 76,026, withdrawal 3,397, removal 702,
# partial_retraction 2). Each reads as retracted, its notice DOI as retraction_doi.
_RETRACTIONS = frozenset({"retraction", "partial_retraction", "withdrawal", "removal"})
_CONCERN = "expression_of_concern"
# Crossref polite-pool etiquette: identify the client (no personal email).
_HEADERS = {
    "User-Agent": "data-aggregator-mcp (+https://github.com/musharna/data-aggregator-mcp)",
    "Accept": "application/json",
}
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


_DESCRIBED_IN = "described_in"
_DOI_RESOLVER_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _as_doi(value: str) -> str | None:
    """A bare DOI from a link target (bare / ``doi:`` / resolver-URL form), or None for a
    non-DOI target such as ``plant-genomics:taxid:4081``."""
    v = value.strip()
    for prefix in _DOI_RESOLVER_PREFIXES:
        if v.lower().startswith(prefix):
            v = v[len(prefix) :]
            break
    return v if _DOI_RE.match(v) else None


def _dois_of(resource: DataResource) -> list[str]:
    """The record's own DOI, then each described_in paper DOI; case-insensitively unique."""
    out: list[str] = []
    own = resource.doi or resource.identifiers.get("doi")
    candidates = [own] + [
        _as_doi(lnk.target_id) for lnk in resource.links if lnk.rel == _DESCRIBED_IN
    ]
    for d in candidates:
        if d and d.lower() not in {x.lower() for x in out}:
            out.append(d)
    return out


def _update_type(update: dict) -> str:
    """An update's type in Crossref's schema spelling. The live index also spells some
    ``Retraction`` (3) and ``expression-of-concern`` (6), facet 2026-10-02."""
    return update["type"].lower().replace("-", "_")


def _is_update(u: object) -> bool:
    return isinstance(u, dict) and isinstance(u.get("type"), str)


def _check_work(body: dict) -> None:
    """A Crossref work: a ``message`` with a DOI, and ``updated-by`` absent or a list of
    typed updates. Anything else would read as "no retraction" without being one."""
    message = body.get("message")
    if not (
        isinstance(message, dict)
        and isinstance(message.get("DOI"), str)
        and isinstance(updates := message.get("updated-by", []), list)
        and all(_is_update(u) for u in updates)
    ):
        raise _http.UpstreamEnvelopeError(f"no Crossref work in {body!r:.200}")


# Outcome of one Crossref lookup: a found work's signals, "absent" (not a Crossref work),
# or "failed" (outage / unparseable) — kept apart so a failure never reads as clean.
_Lookup = TrustSignals | Literal["absent", "failed"]


async def annotate(client: httpx.AsyncClient, resource: DataResource) -> TrustSignals:
    dois = _dois_of(resource)
    if not dois:
        return TrustSignals()  # nothing to check → unknown
    results: list[_Lookup] = list(await asyncio.gather(*(_check(client, d) for d in dois)))
    found = [r for r in results if isinstance(r, TrustSignals)]
    failed = "failed" in results
    retracted = next((r for r in found if r.retracted), None)
    if retracted is not None:
        return TrustSignals(
            retracted=True,
            retraction_doi=retracted.retraction_doi,
            concern=True if any(r.concern for r in found) else (None if failed else False),
        )
    if failed or not found:
        return TrustSignals()  # a lookup failed, or no DOI is a Crossref work → unknown
    return TrustSignals(retracted=False, concern=any(r.concern for r in found))


async def _check(client: httpx.AsyncClient, doi: str) -> _Lookup:
    """One Crossref /works/{doi} lookup. Enrichment: never raises."""
    try:
        body = await _http.request_json(
            client,
            "GET",
            CROSSREF.format(doi=_http.doi_path(doi)),
            service="Crossref retraction",
            headers=_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            not_found_returns=None,  # 404 → not a Crossref work → unknown
            expect=dict,
            check=_check_work,
        )
        if body is None:
            return "absent"
        updates = body["message"].get("updated-by", [])
        notice = next((u for u in updates if _update_type(u) in _RETRACTIONS), None)
        return TrustSignals(
            retracted=notice is not None,
            retraction_doi=notice.get("DOI") if notice else None,
            concern=any(_update_type(u) == _CONCERN for u in updates),
        )
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise into a valid resolve (spec §8)
        logger.warning("trust annotate failed for %s: %r", doi, exc)
        return "failed"
