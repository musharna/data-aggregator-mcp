"""Citation rendering for resolved records — DOI content negotiation + CSL-JSON fallback.

DOI-bearing records render via DOI content negotiation (GET https://doi.org/<doi>, the
CrossCite mechanism covering CrossRef + DataCite); no CSL engine is bundled. Non-DOI
records still produce CSL-JSON from our normalized metadata. This is enrichment: any
failure logs a warning and returns None — it never raises into a valid resolve (spec §8).
"""

from __future__ import annotations

import json
import logging

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource

logger = logging.getLogger(__name__)

DOI_BASE = "https://doi.org"
# httpx upper-cases the method and reads header names case-insensitively, so these are
# constants: any spelling of them sends the same request (tests/test_citation_observed.py).
_GET = "GET"
_ACCEPT = "Accept"
_CONTENT_TYPE = "Content-Type"

# Structured formats with a dedicated content-negotiation MIME; any other value is
# treated as a CSL style name rendered as a text bibliography (apa, mla, vancouver, ...).
_FORMAT_ACCEPT = {
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
    "csl-json": "application/vnd.citationstyles.csl+json",
}

# DataResource.kind -> CSL-JSON type
_CSL_TYPE = {
    "publication": "article-journal",
    "dataset": "dataset",
    "software": "software",
    "study": "dataset",
    "sequencing_run": "dataset",
}


def _doi_url(doi: str) -> str:
    """doi.org URL for ``doi`` with the DOI percent-encoded as a path (``/`` kept). DOIs
    may legally contain ``#``, ``?``, ``<``, ``>`` and ``;`` (SICI DOIs do); sent raw, a
    ``#`` cut the DOI short as a URL fragment and a ``?`` would become a query string."""
    return f"{DOI_BASE}/{_http.doi_path(doi)}"


# A CSL style is asked for as a text bibliography. KISTI answers it as text/plain
# (probed 2026-10-02), which is the same plain-text reference.
_STYLE_MEDIA = "text/x-bibliography"
_STYLE_ANSWERS = frozenset({_STYLE_MEDIA, "text/plain"})


def _accept_for(fmt: str) -> str:
    return _FORMAT_ACCEPT.get(fmt) or f"{_STYLE_MEDIA}; style={fmt}"


def _answers_for(fmt: str) -> frozenset[str]:
    """The media types that carry ``fmt``. A registration agency without content
    negotiation for ``fmt`` answers 200 in some other type: Airiti sends CSL-JSON for every
    format, and ISTIC redirects to its HTML landing page (both probed 2026-10-02)."""
    media = _FORMAT_ACCEPT.get(fmt)
    return frozenset({media}) if media else _STYLE_ANSWERS


def _media_type(resp: httpx.Response) -> str:
    """The response's media type, without parameters, lower-cased (RFC 9110 §8.3.1:
    type and subtype are case-insensitive; KISTI sends ``application/x-Research-Info-Systems``)."""
    return resp.headers.get(_CONTENT_TYPE, "").partition(";")[0].strip().lower()


def _csl_json_from_metadata(r: DataResource) -> str:
    item: dict = {"id": r.id, "type": _CSL_TYPE.get(r.kind, "dataset"), "title": r.title}
    if r.creators:
        item["author"] = [{"literal": c.name} for c in r.creators]
    if r.year:
        item["issued"] = {"date-parts": [[r.year]]}
    return json.dumps(item)  # only records without a DOI get here: render asks doi.org


async def render(client: httpx.AsyncClient, resource: DataResource, fmt: str) -> str | None:
    """Render a citation for ``resource`` in ``fmt``. DOI records use DOI content
    negotiation; non-DOI records yield CSL-JSON from metadata only. Fail soft: any
    failure logs a warning and returns None — this enrichment never raises (spec §8)."""
    fmt = fmt.strip().lower()
    if not fmt:
        return None
    try:
        if not resource.doi:
            if fmt == "csl-json":
                return _csl_json_from_metadata(resource)
            logger.warning("citation: format %r needs a DOI; %s has none", fmt, resource.id)
            return None
        if not resource.doi.isprintable():
            # No DOI contains a control character; quoting one would turn a malformed
            # upstream value into a real request instead of a refusal.
            logger.warning("citation: DOI of %s is malformed: %r", resource.id, resource.doi)
            return None
        resp = await _http.request_with_retry(
            client,
            _GET,
            _doi_url(resource.doi),
            service="DOI content negotiation",
            headers={_ACCEPT: _accept_for(fmt)},
        )
        media = _media_type(resp)
        if media not in _answers_for(fmt):
            logger.warning(
                "citation: doi.org has no %r citation of %s (%s); it answered %r",
                fmt,
                resource.doi,
                resource.id,
                media,
            )
            return None
        return resp.text.strip() or None
    except Exception as exc:  # noqa: BLE001 — enrichment contract: never raise (spec §8)
        logger.warning("citation render failed for %s (%s): %r", resource.id, fmt, exc)
        return None
