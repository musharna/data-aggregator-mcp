"""Unified literature source — fans out to PubMed + OpenAIRE backends.

One router source (mirrors the Phase 3 omics adapter). ``search`` runs both
backends in parallel and round-robin-merges; ``resolve`` routes by id prefix.
Discovery-only: no fetch, no citation-graph analysis (that is the openalex MCP).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from data_aggregator_mcp import openaire, pubmed
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, compact

logger = logging.getLogger(__name__)

_BACKENDS = {"pubmed": pubmed, "openaire": openaire}
PREFIXES = tuple(_BACKENDS)  # ("pubmed", "openaire") — derived so it can't drift
DEFAULT_SIZE = 10
MAX_SIZE = 50


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
) -> tuple[int, list[DataResource]]:
    """Discover across PubMed + OpenAIRE. Returns (summed_total, COMPACT)."""
    capped = min(size, MAX_SIZE)
    outcomes = await asyncio.gather(
        *(b.search(client, query, size=capped) for b in _BACKENDS.values()),
        return_exceptions=True,
    )
    total = 0
    per_backend: list[list[DataResource]] = []
    for name, outcome in zip(_BACKENDS, outcomes):
        if isinstance(outcome, Exception):
            logger.warning("literature search: %s backend failed: %r", name, outcome)
            continue
        backend_total, recs = outcome
        total += backend_total
        per_backend.append(recs)
    merged = interleave(per_backend)[:capped]
    return total, [compact(r) for r in merged]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Route ``pubmed:<PMID>`` / ``openaire:<id>`` to the matching backend."""
    prefix, _, _ = resource_id.partition(":")
    backend = _BACKENDS.get(prefix)
    if backend is None:
        raise NotFoundError(f"unroutable literature id {resource_id!r}")
    return await backend.resolve(client, resource_id)
