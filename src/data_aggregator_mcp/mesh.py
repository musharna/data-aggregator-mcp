"""NCBI MeSH lookups: disease/phenotype name -> canonical descriptor + synonyms.

Internal helper (NOT a router adapter). Backs P4.1 disease synonym expansion
(search input). One source: esearch db=mesh field-restricted to ``[MeSH Terms]``
resolves a lay name to its one canonical MeSH descriptor UID (the field
restriction is load-bearing — a plain relevance search returns a narrower
subtype as the top hit). esummary (JSON, version 2.0) then yields the canonical
descriptor name (``ds_meshterms[0]``), entry-term synonyms (``ds_meshterms[1:]``)
and the MeSH UI (``ds_meshui``). Results are cached in-process keyed by
lowercased name. Mirrors ``taxonomy.py`` but uses the JSON esummary helpers
(simpler than the XML efetch path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from data_aggregator_mcp import _eutils
from data_aggregator_mcp._cache import MISS, TTLCache


@dataclass(frozen=True)
class MeshInfo:
    ui: str  # MeSH descriptor UI, e.g. "D001943"
    canonical: str  # ds_meshterms[0]
    synonyms: tuple[str, ...]  # ds_meshterms[1:] (entry-term synonyms)


def _parse_mesh(docs: list[dict[str, Any]]) -> MeshInfo | None:
    """Parse a MeSH esummary result; accept only real ``descriptor`` records.

    Qualifiers and supplementary concept records (SCRs) are not valid
    query-expansion anchors → None. A descriptor with no ``ds_meshterms`` or a
    falsy ``ds_meshui`` is likewise rejected.
    """
    if not docs:
        return None
    doc = docs[0]
    if doc.get("ds_recordtype") != "descriptor":
        return None
    terms = doc.get("ds_meshterms") or []
    if not terms:
        return None
    ui = doc.get("ds_meshui")
    if not ui:
        return None
    return MeshInfo(ui=ui, canonical=terms[0], synonyms=tuple(terms[1:]))


_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


async def resolve_mesh(client: httpx.AsyncClient, name: str) -> MeshInfo | None:
    """Resolve a disease/phenotype ``name`` to a ``MeshInfo`` (or None if no match).

    Cached in-process by lowercased name (negative results cached too) so
    repeated diseases in one request cost a single NCBI round-trip pair. HTTP
    failures propagate (the caller surfaces them); they are NOT cached.
    """
    key = name.strip().lower()
    if not key:
        return None
    cached = _CACHE.get(key)
    if cached is not MISS:
        return None if cached is _NEG else cached
    _count, ids = await _eutils.esearch(client, "mesh", f"{name}[MeSH Terms]", retmax=1)
    if not ids:
        _CACHE.set(key, _NEG)
        return None
    docs = await _eutils.esummary(client, "mesh", [ids[0]])
    info = _parse_mesh(docs)
    _CACHE.set(key, info if info is not None else _NEG)
    return info
