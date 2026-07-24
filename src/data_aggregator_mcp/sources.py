"""Central source registry — one ordered list of wired sources and their routing +
fetchability, from which ``router._ADAPTERS`` / ``router._DISCOVERY_ONLY_SOURCES`` and
``server._FETCHABLE_SOURCES`` all derive.

This module depends on the adapters but on neither ``router`` nor ``server``, so both can
import it without a cycle. Before this, per-source routing/fetchability was restated in
four places kept in lockstep only by tests; a miss produced the ``uniprot`` bug (registered
+ fetchable, but absent from the resolve dispatch chain — resolve/fetch unreachable). Adding
a source is now one row here.

Not (yet) owned here: the rich human-facing ``server._SOURCES`` metadata (layer / kinds /
rate_limit / description / …). It stays in ``server`` but is consistency-checked against this
registry by a test, so ``name`` and fetchability can't drift.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from data_aggregator_mcp import (
    biostudies,
    cellxgene,
    dandi,
    datacite,
    datagov,
    dataone,
    gbif,
    gwas,
    huggingface,
    literature,
    nasacmr,
    omics,
    omicsdi,
    openml,
    pdb,
    uniprot,
    zenodo,
)


@dataclass(frozen=True)
class SourceSpec:
    """One wired source. ``prefixes`` are every id prefix it resolves; ``fetchable_prefixes``
    is the subset with a working fetch backend (usually all of them — but e.g. omics routes
    ``bioproject`` for discovery yet only ``geo``/``sra`` are fetchable). A source is
    discovery-only (no fetch, lowest DOI-dedup precedence) exactly when
    ``fetchable_prefixes`` is empty."""

    name: str
    module: Any
    prefixes: frozenset[str]
    fetchable_prefixes: frozenset[str]


def _spec(
    name: str,
    module: Any,
    *,
    fetchable: bool = True,
    fetchable_prefixes: Collection[str] | None = None,
) -> SourceSpec:
    prefixes = frozenset(module.PREFIXES)
    if not fetchable:
        fp: frozenset[str] = frozenset()
    elif fetchable_prefixes is None:
        fp = prefixes
    else:
        fp = frozenset(fetchable_prefixes)
    return SourceSpec(name=name, module=module, prefixes=prefixes, fetchable_prefixes=fp)


# Order = _dedup merge precedence: fetchable natives before DataCite (so on a shared DOI the
# fetchable copy is seen first); discovery-only sources (gwas/nasacmr) registered late so a
# fetchable native still wins. See router._fetch_priority for the collision rule itself.
SOURCES: tuple[SourceSpec, ...] = (
    _spec("zenodo", zenodo),
    _spec("dataone", dataone),
    _spec(
        "gbif", gbif
    ),  # GBIF DOIs share the 10.15468 DataCite prefix — native must precede datacite
    _spec("datagov", datagov),
    _spec("cellxgene", cellxgene),
    _spec("datacite", datacite),
    _spec("dandi", dandi),
    _spec("omics", omics, fetchable_prefixes={"geo", "sra"}),  # bioproject routable, not fetchable
    _spec("literature", literature),
    _spec("huggingface", huggingface),
    _spec("omicsdi", omicsdi),
    _spec("openml", openml),
    _spec("pdb", pdb),
    _spec("uniprot", uniprot),
    _spec("gwas", gwas, fetchable=False),  # discovery-only
    _spec("nasacmr", nasacmr, fetchable=False),  # discovery-only
    _spec("biostudies", biostudies),
)

# Name → adapter module, in registration (precedence) order.
ADAPTERS: dict[str, Any] = {s.name: s.module for s in SOURCES}

# Sources with no fetch backend at all — lowest DOI-dedup precedence (router._fetch_priority).
DISCOVERY_ONLY: frozenset[str] = frozenset(s.name for s in SOURCES if not s.fetchable_prefixes)

# Fetch-gate: id prefixes with a working fetch backend (server._is_fetchable checks startswith).
FETCHABLE_PREFIXES: tuple[str, ...] = tuple(
    f"{p}:" for s in SOURCES for p in sorted(s.fetchable_prefixes)
)


def resolver_for(prefix: str) -> Any | None:
    """The adapter module that resolves ids with ``prefix``, or None if unrouted.
    (Bare-numeric → zenodo and bare-DOI → datacite are handled by the caller.)"""
    for spec in SOURCES:
        if prefix in spec.prefixes:
            return spec.module
    return None
