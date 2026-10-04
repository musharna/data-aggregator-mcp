"""UBERON tissue/anatomy lookups: tissue name -> canonical label + exact synonyms.

Internal helper (NOT a router adapter). Backs the ``tissue=`` search-input
expansion — the third ontology-grounded recall axis after ``organism=`` (NCBI
Taxonomy) and ``disease=`` (MeSH). This is the FIRST expansion backed by a
NON-NCBI client: it asks EBI OLS4 for the UBERON terms whose label or a synonym is
the name, through ``_ols.exact_search`` (shared with ``chemistry``; it says why
``queryFields`` is needed and what counts as a malformed answer).

TWO client-side filters in ``_pick_uberon`` are load-bearing:

1. ``obo_id`` must start with ``"UBERON:"``. ``ontology=uberon`` does NOT
   hard-restrict — ``q=hepar`` leaks ``PR:`` (Protein Ontology) terms.
2. An exact (case-insensitive) match of the input to ``label`` OR an entry in
   ``synonym`` is required. It is the definition of a match, so a looser
   upstream match cannot expand into a wrong term (``exact=true`` alone
   matched other fields too: ``q=liver`` returned "caudate lobe of liver").

No exact match → None (conservative: never expand into a wrong term). Results
are cached in-process keyed by lowercased name (negative results too). HTTP
failures propagate (the caller surfaces them in ``errors``); they are NOT cached.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import httpx

from data_aggregator_mcp import _ols
from data_aggregator_mcp._cache import MISS, TTLCache


@dataclass(frozen=True)
class UberonInfo:
    uberon_id: str  # e.g. "UBERON:0002107"
    canonical: str  # OLS label
    synonyms: tuple[str, ...]  # OLS ``synonym`` entries (exact and related alike)
    # the other terms the name matched, in OLS's order (``_ols.choose`` says which wins)
    alternatives: tuple[_ols.Alternative, ...] = ()


_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


def _pick_uberon(docs: list[dict[str, Any]], key: str) -> UberonInfo | None:
    """Pure matcher: select the canonical UBERON doc whose label OR a synonym
    matches ``key`` (lowercased input) exactly. Both hard filters from the module
    docstring are applied; ``_ols.choose`` picks among several (label match, then
    the defining ontology, then OLS's order) and the rest are kept as
    ``alternatives``. Returns None when no candidate matches (conservative — no
    expansion is preferred over a wrong term).
    """
    candidates: list[tuple[bool, bool, UberonInfo]] = []
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
        info = UberonInfo(uberon_id=obo_id, canonical=label, synonyms=synonyms)
        candidates.append((label.lower() == key, doc.get("is_defining_ontology") is True, info))
    if not candidates:
        return None
    chosen = _ols.choose([(is_label, is_defining) for is_label, is_defining, _info in candidates])
    others = tuple(
        _ols.Alternative(info.uberon_id, info.canonical)
        for i, (_is_label, _is_defining, info) in enumerate(candidates)
        if i != chosen
    )
    return replace(candidates[chosen][2], alternatives=others)


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
    docs = await _ols.exact_search(client, name, ontology="uberon", service="EBI OLS (UBERON)")
    info = _pick_uberon(docs, key)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
