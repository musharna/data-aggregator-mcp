"""EDAM assay/method lookups: assay name -> canonical label + exact synonyms.

Internal helper (NOT a router adapter). Backs the ``assay=`` search-input
expansion — the fifth ontology-grounded recall axis after ``organism=`` (NCBI
Taxonomy), ``disease=`` (MeSH), ``tissue=`` (UBERON) and ``chemical=`` (ChEBI).
A near-verbatim clone of ``anatomy.py``: it queries the same EBI OLS4 search API
(``GET /ols4/api/search?ontology=edam&exact=true``) via the shared
``_http.request_json`` helper.

EDAM is the right backend for the assay/method axis (OBI returns the terms but
with empty ``synonym`` — zero recall value). TWO client-side filters in
``_pick_edam`` are load-bearing (both proven by a live probe — neither OLS param
self-enforces):

1. ``obo_id`` must start with ``"EDAM:topic_"``. EDAM mixes id-classes
   (``topic_``, ``data_``, ``format_``, ``operation_``); assay/method concepts
   are EDAM *topics*, so ``data_``/``format_``/``operation_`` are rejected.
2. An exact (case-insensitive) match of the input to ``label`` OR an entry in
   ``synonym`` is required. ``exact=true`` does NOT hard-filter.

No synonym cap needed (EDAM lists are small). No exact match → None
(conservative: never expand into a wrong term). Results are cached in-process
keyed by lowercased name (negative results too). HTTP failures propagate (the
caller surfaces them in ``errors``); they are NOT cached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._cache import MISS, TTLCache


@dataclass(frozen=True)
class EdamInfo:
    edam_id: str  # e.g. "EDAM:topic_3169"
    canonical: str  # OLS label
    synonyms: tuple[str, ...]  # exact entry synonyms (excludes the canonical label)


OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
_HEADERS = {"User-Agent": "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"}

_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


def _pick_edam(docs: list[dict[str, Any]], key: str) -> EdamInfo | None:
    """Pure matcher: select the canonical EDAM-topic doc whose label OR a synonym
    matches ``key`` (lowercased input) exactly. Both hard filters from the module
    docstring are applied (id-class restricted to ``EDAM:topic_``);
    ``is_defining_ontology is True`` breaks ties (else the first candidate).
    Returns None when no candidate matches (conservative — no expansion is
    preferred over a wrong term).
    """
    candidates: list[EdamInfo] = []
    defining: list[bool] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        obo_id = doc.get("obo_id")
        if not isinstance(obo_id, str) or not obo_id.startswith("EDAM:topic_"):
            continue
        if doc.get("is_obsolete"):
            continue
        label = doc.get("label")
        if not isinstance(label, str):
            continue
        # OLS is Solr-backed: a single-valued ``synonym`` can come back as a bare
        # string, not a list. Iterating a string would explode it into characters
        # (and break the exact-synonym match), so normalize scalar → [scalar].
        raw_synonyms = doc.get("synonym")
        if isinstance(raw_synonyms, str):
            raw_synonyms = [raw_synonyms]
        elif not isinstance(raw_synonyms, list):
            raw_synonyms = []
        synonyms = tuple(s for s in raw_synonyms if isinstance(s, str) and s.strip())
        synset = {s.lower() for s in synonyms}
        if label.lower() != key and key not in synset:
            continue
        candidates.append(EdamInfo(edam_id=obo_id, canonical=label, synonyms=synonyms))
        defining.append(doc.get("is_defining_ontology") is True)
    if not candidates:
        return None
    for info, is_defining in zip(candidates, defining, strict=False):
        if is_defining:
            return info
    return candidates[0]


async def resolve_edam(client: httpx.AsyncClient, name: str) -> EdamInfo | None:
    """Resolve an assay/method ``name`` to an ``EdamInfo`` (or None if no exact
    EDAM-topic match). Cached in-process by lowercased name (negative results
    cached too) so repeated assays in one request cost a single OLS round-trip.
    HTTP failures propagate (the caller surfaces them); they are NOT cached.
    """
    key = name.strip().lower()
    if not key:
        return None
    cached = _CACHE.get(key)
    if cached is not MISS:
        return None if cached is _NEG else cached
    body = await _http.request_json(
        client,
        "GET",
        OLS_SEARCH,
        service="EBI OLS (EDAM)",
        headers=_HEADERS,
        params={
            "q": name,
            "ontology": "edam",
            "exact": "true",
            "fieldList": "obo_id,label,synonym,is_defining_ontology,is_obsolete",
            "rows": "10",
        },
        timeout=30.0,
        max_retries=2,
    )
    response = (body or {}).get("response") if isinstance(body, dict) else None
    docs = response.get("docs") if isinstance(response, dict) else None
    info = _pick_edam(docs if isinstance(docs, list) else [], key)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
