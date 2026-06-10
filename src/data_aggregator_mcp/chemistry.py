"""ChEBI chemical/compound lookups: compound name -> canonical label + exact synonyms.

Internal helper (NOT a router adapter). Backs the ``chemical=`` search-input
expansion — the fourth ontology-grounded recall axis after ``organism=`` (NCBI
Taxonomy), ``disease=`` (MeSH) and ``tissue=`` (UBERON). A near-verbatim clone
of ``anatomy.py``: it queries the same EBI OLS4 search API
(``GET /ols4/api/search?ontology=chebi&exact=true``) via the shared
``_http.request_json`` helper.

TWO client-side filters in ``_pick_chebi`` are load-bearing (both proven by a
live probe — neither OLS param self-enforces):

1. ``obo_id`` must start with ``"CHEBI:"``. ``ontology=chebi`` does NOT
   hard-restrict cross-ontology leaks.
2. An exact (case-insensitive) match of the input to ``label`` OR an entry in
   ``synonym`` is required. ``exact=true`` does NOT hard-filter — the top
   relevance hit is not guaranteed canonical (``q=aspirin`` returns
   "aspirin-triggered protectin D1" before the real ``aspirin``/``CHEBI:15365``).

ChEBI realism: synonym lists are large (many IUPAC variants), so the matched
canonical's synonyms are capped to ``_MAX_SYNONYMS = 12`` exact synonyms (the
canonical label is always retained) to bound the OR-group query size. UBERON
and MeSH need no cap.

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
class ChebiInfo:
    chebi_id: str  # e.g. "CHEBI:27732"
    canonical: str  # OLS label
    synonyms: tuple[str, ...]  # exact entry synonyms (excludes the canonical label), capped


OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
_HEADERS = {"User-Agent": "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"}

_MAX_SYNONYMS = 12  # ChEBI synonym lists are large; cap the OR-group to a sane size.

_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


def _pick_chebi(docs: list[dict[str, Any]], key: str) -> ChebiInfo | None:
    """Pure matcher: select the canonical ChEBI doc whose label OR a synonym
    matches ``key`` (lowercased input) exactly. Both hard filters from the module
    docstring are applied; ``is_defining_ontology is True`` breaks ties (else the
    first candidate). The matched doc's synonyms are capped to ``_MAX_SYNONYMS``
    (canonical always kept). Returns None when no candidate matches (conservative —
    no expansion is preferred over a wrong term).
    """
    candidates: list[ChebiInfo] = []
    defining: list[bool] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        obo_id = doc.get("obo_id")
        if not isinstance(obo_id, str) or not obo_id.startswith("CHEBI:"):
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
        all_synonyms = [s for s in raw_synonyms if isinstance(s, str) and s.strip()]
        synset = {s.lower() for s in all_synonyms}
        if label.lower() != key and key not in synset:
            continue
        # ChEBI returns many case/format variants of the same term (e.g. "CAFFEINE",
        # "Caffeine", "caffeine") plus the canonical label itself. Dedup
        # case-insensitively and drop the canonical before capping, so the bounded
        # _MAX_SYNONYMS budget holds DISTINCT recall terms, not wasted duplicates.
        seen = {label.lower()}
        distinct: list[str] = []
        for s in all_synonyms:
            low = s.lower()
            if low in seen:
                continue
            seen.add(low)
            distinct.append(s)
        candidates.append(
            ChebiInfo(chebi_id=obo_id, canonical=label, synonyms=tuple(distinct[:_MAX_SYNONYMS]))
        )
        defining.append(doc.get("is_defining_ontology") is True)
    if not candidates:
        return None
    for info, is_defining in zip(candidates, defining, strict=False):
        if is_defining:
            return info
    return candidates[0]


async def resolve_chebi(client: httpx.AsyncClient, name: str) -> ChebiInfo | None:
    """Resolve a chemical/compound ``name`` to a ``ChebiInfo`` (or None if no exact
    ChEBI match). Cached in-process by lowercased name (negative results cached
    too) so repeated chemicals in one request cost a single OLS round-trip. HTTP
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
        service="EBI OLS (ChEBI)",
        headers=_HEADERS,
        params={
            "q": name,
            "ontology": "chebi",
            "exact": "true",
            "fieldList": "obo_id,label,synonym,is_defining_ontology,is_obsolete",
            "rows": "10",
        },
        timeout=30.0,
        max_retries=2,
    )
    response = (body or {}).get("response") if isinstance(body, dict) else None
    docs = response.get("docs") if isinstance(response, dict) else None
    info = _pick_chebi(docs if isinstance(docs, list) else [], key)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
