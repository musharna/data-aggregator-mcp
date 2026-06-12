"""Optional semantic re-rank via a remote OpenAI-compatible embeddings endpoint.

Disabled (returns None) unless ``EMBEDDING_API_BASE`` is set. NEVER raises into
the search path — any failure degrades to keyword order. No local model, no
required key (a keyless local server is supported by omitting the auth header).

NOTE: ``_http`` has no ``json=`` param, so we serialize the JSON ourselves, pass
it as ``content=`` (httpx's non-deprecated raw-body param), and set Content-Type
explicitly.
"""

from __future__ import annotations

import json
import math
import os

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource

_MAX_CHARS = 2000


def _config() -> tuple[str, str | None, str] | None:
    base = os.environ.get("EMBEDDING_API_BASE")
    if not base:
        return None
    return (
        base.rstrip("/"),
        os.environ.get("EMBEDDING_API_KEY"),
        os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
    )


def is_configured() -> bool:
    """True if an embedding endpoint is configured (``EMBEDDING_API_BASE`` set), so
    ``rank=semantic`` / semantic re-rank can actually run. Pure env read — no network."""
    return _config() is not None


async def embed(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]] | None:
    cfg = _config()
    if cfg is None:
        return None
    base, key, model = cfg
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps({"model": model, "input": texts})
    try:
        body = await _http.request_json(
            client,
            "POST",
            f"{base}/embeddings",
            service="embeddings",
            content=payload,
            headers=headers,
        )
        return [row["embedding"] for row in body["data"]]
    except Exception:  # unavailable / malformed — caller degrades to keyword order
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0  # zero-norm sorts last
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


def cosine_rank(query_vec: list[float], cand_vecs: list[list[float]]) -> list[int]:
    """Indices of candidates sorted by descending cosine similarity to the query
    (ties broken by original order)."""
    scored = sorted(
        range(len(cand_vecs)),
        key=lambda i: (-_cosine(query_vec, cand_vecs[i]), i),
    )
    return scored


async def rerank(
    client: httpx.AsyncClient, query: str, resources: list[DataResource]
) -> tuple[list[DataResource], str | None]:
    """Re-order ``resources`` by semantic similarity to ``query``. On success
    returns ``(reordered, None)``; if embeddings are unavailable or fail, returns
    ``(resources_unchanged, reason)``."""
    if not resources:
        return resources, None
    texts = [f"{r.title or ''}\n{r.description or ''}"[:_MAX_CHARS] for r in resources]
    vecs = await embed(client, [query, *texts])
    if vecs is None or len(vecs) != len(resources) + 1:
        return resources, (
            "semantic re-rank unavailable (no embedding endpoint configured or embed failed)"
        )
    order = cosine_rank(vecs[0], vecs[1:])
    return [resources[i] for i in order], None
