"""UBERON tissue/anatomy lookups: tissue name -> canonical label + exact synonyms.

Internal helper (NOT a router adapter). Backs the ``tissue=`` search-input
expansion — the third ontology-grounded recall axis after ``organism=`` (NCBI
Taxonomy) and ``disease=`` (MeSH). This is the FIRST expansion backed by a
NON-NCBI client: it queries the EBI OLS4 search API
(``GET /ols4/api/search?ontology=uberon&exact=true``) via the shared
``_http.request_json`` helper rather than NCBI E-utilities.

TWO client-side filters in ``_pick_uberon`` are load-bearing (both proven by a
live probe — neither OLS param self-enforces):

1. ``obo_id`` must start with ``"UBERON:"``. ``ontology=uberon`` does NOT
   hard-restrict — ``q=hepar`` leaks ``PR:`` (Protein Ontology) terms.
2. An exact (case-insensitive) match of the input to ``label`` OR an entry in
   ``synonym`` is required. ``exact=true`` does NOT hard-filter — the top
   relevance hit is not guaranteed canonical (``q=liver`` also returns
   "caudate lobe of liver").

No exact match → None (conservative: never expand into a wrong term). Results
are cached in-process keyed by lowercased name (negative results too). HTTP
failures propagate (the caller surfaces them in ``errors``); they are NOT cached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._cache import MISS, TTLCache


@dataclass(frozen=True)
class UberonInfo:
    uberon_id: str  # e.g. "UBERON:0002107"
    canonical: str  # OLS label
    synonyms: tuple[str, ...]  # exact entry synonyms (excludes the canonical label)


OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
_HEADERS = {"User-Agent": "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"}

_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


def _pick_uberon(docs: list[dict[str, Any]], key: str) -> UberonInfo | None:
    """Pure matcher: select the canonical UBERON doc whose label OR a synonym
    matches ``key`` (lowercased input) exactly. Both hard filters from the module
    docstring are applied; ``is_defining_ontology is True`` breaks ties (else the
    first candidate). Returns None when no candidate matches (conservative — no
    expansion is preferred over a wrong term).
    """
    candidates: list[UberonInfo] = []
    defining: list[bool] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        obo_id = doc.get("obo_id")
        if not isinstance(obo_id, str) or not obo_id.startswith("UBERON:"):
            continue
        if doc.get("is_obsolete"):
            continue
        label = doc.get("label")
        if not isinstance(label, str):
            continue
        raw_synonyms = doc.get("synonym") or []
        synonyms = tuple(s for s in raw_synonyms if isinstance(s, str) and s.strip())
        synset = {s.lower() for s in synonyms}
        if label.lower() != key and key not in synset:
            continue
        candidates.append(UberonInfo(uberon_id=obo_id, canonical=label, synonyms=synonyms))
        defining.append(doc.get("is_defining_ontology") is True)
    if not candidates:
        return None
    for info, is_defining in zip(candidates, defining, strict=False):
        if is_defining:
            return info
    return candidates[0]


async def resolve_uberon(client: httpx.AsyncClient, name: str) -> UberonInfo | None:
    """Resolve a tissue/anatomy ``name`` to a ``UberonInfo`` (or None if no exact
    UBERON match). Cached in-process by lowercased name (negative results cached
    too) so repeated tissues in one request cost a single OLS round-trip. HTTP
    failures propagate (the caller surfaces them); they are NOT cached.
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
        service="EBI OLS (UBERON)",
        headers=_HEADERS,
        params={
            "q": name,
            "ontology": "uberon",
            "exact": "true",
            "fieldList": "obo_id,label,synonym,is_defining_ontology,is_obsolete",
            "rows": "10",
        },
        timeout=30.0,
        max_retries=2,
    )
    response = (body or {}).get("response") if isinstance(body, dict) else None
    docs = response.get("docs") if isinstance(response, dict) else None
    info = _pick_uberon(docs if isinstance(docs, list) else [], key)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
