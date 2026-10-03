"""EBI OLS4 exact-name search, shared by the ontology lookups (``anatomy``, ``chemistry``).

One request, one answer check, so a fix to either reaches every lookup that uses it.

``queryFields=label,synonym`` is what makes ``exact=true`` exact. Without it OLS
matches the query against its default fields (description, annotations, ...) and
ranks by relevance, so the term whose label or synonym IS the name can sit below
the window: live on 2026-10-02 ChEBI ranked ``acetylsalicylic acid`` (synonym
``aspirin``) 18th of 26, and UBERON ranked the first ``skin`` match 22nd of 346 and
the first ``bone`` match 68th of 1,705, so all three resolved as "no match". With
it, every doc OLS returns has the name as its label or a synonym (case-insensitive),
the label match first. The name is sent stripped: an exact match on ``" liver "``
finds nothing.

OLS answers a search with ``{"response": {"docs": [...], "numFound": N, ...}}``. A
200 without that list, or with a doc whose fields are not the types the lookups
read, is upstream trouble (retried, then ``UpstreamUnavailableError``), never "no
match", which the lookups cache.
"""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http

SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
_HEADERS = {"User-Agent": "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"}
_GET = "GET"
_QUERY = {
    "exact": "true",
    "queryFields": "label,synonym",
    "fieldList": "obo_id,label,synonym,is_defining_ontology,is_obsolete",
    "rows": "10",
}


def _is_doc(doc: Any) -> bool:
    """A search doc carries a string ``obo_id`` and ``label``; ``synonym`` (a list of
    strings, or one string) and ``is_defining_ontology`` (a bool) may be absent."""
    if not isinstance(doc, dict):
        return False
    synonym = doc.get("synonym")
    return (
        isinstance(doc.get("obo_id"), str)
        and isinstance(doc.get("label"), str)
        and (
            isinstance(synonym, str | None)
            or (isinstance(synonym, list) and all(isinstance(s, str) for s in synonym))
        )
        and isinstance(doc.get("is_defining_ontology"), bool | None)
    )


def _check_search(body: dict) -> None:
    response = body.get("response")
    docs = response.get("docs") if isinstance(response, dict) else None
    if not (isinstance(docs, list) and all(_is_doc(d) for d in docs)):
        raise _http.UpstreamEnvelopeError(f"no OLS search docs in {body!r:.200}")


async def exact_search(
    client: httpx.AsyncClient, name: str, *, ontology: str, service: str
) -> list[dict[str, Any]]:
    """The docs whose label or a synonym is ``name`` (case-insensitive) in ``ontology``."""
    body = await _http.request_json(
        client,
        _GET,
        SEARCH,
        service=service,
        headers=_HEADERS,
        params={"q": name.strip(), "ontology": ontology, **_QUERY},
        max_retries=2,
        expect=dict,
        check=_check_search,
    )
    docs: list[dict[str, Any]] = body["response"]["docs"]
    return docs
