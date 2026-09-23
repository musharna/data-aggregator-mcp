"""Unified literature source — fans out to PubMed + OpenAIRE backends.

One router source (mirrors the Phase 3 omics adapter). ``search`` runs both
backends in parallel and round-robin-merges; ``resolve`` routes by id prefix.
Discovery-only: no fetch, no citation-graph analysis (that is the openalex MCP).
"""

from __future__ import annotations

import logging

import httpx

from data_aggregator_mcp import openaire, pubmed
from data_aggregator_mcp._merge import fan_in, interleave
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, compact

logger = logging.getLogger(__name__)

_BACKENDS = {"pubmed": pubmed, "openaire": openaire}
PREFIXES = tuple(_BACKENDS)  # ("pubmed", "openaire") — derived so it can't drift
DEFAULT_SIZE = 10
MAX_SIZE = 50


# The router pages each backend as its own stream (``literature/pubmed`` ...) with its own
# offset; see omics.SUBSOURCES for why one shared offset lost records.
SUBSOURCES = PREFIXES


async def search_subsource(
    client: httpx.AsyncClient,
    subsource: str,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """One backend at its own offset. Raises on failure."""
    total, recs = await _BACKENDS[subsource].search(
        client, query, size=min(size, MAX_SIZE), offset=offset
    )
    return total, [compact(r) for r in recs]


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    """Discover across PubMed + OpenAIRE. Returns (summed_total, COMPACT).

    One page only (``offset`` applies to both backends); the router pages
    ``search_subsource`` per backend. Raises when every backend fails."""
    capped = min(size, MAX_SIZE)
    total, per_backend = await fan_in(
        {n: search_subsource(client, n, query, size=capped, offset=offset) for n in _BACKENDS},
        what="literature search",
        logger=logger,
    )
    return total, interleave(per_backend)[:capped]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Route ``pubmed:<PMID>`` / ``openaire:<id>`` to the matching backend."""
    prefix, _, _ = resource_id.partition(":")
    backend = _BACKENDS.get(prefix)
    if backend is None:
        raise NotFoundError(f"unroutable literature id {resource_id!r}")
    return await backend.resolve(client, resource_id)
