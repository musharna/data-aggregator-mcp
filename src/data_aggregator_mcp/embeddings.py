"""Optional semantic re-rank via a remote OpenAI-compatible embeddings endpoint.

Disabled (returns None) unless ``EMBEDDING_API_BASE`` is set. An unreachable
endpoint, an error status or a malformed answer never raises into the search path:
it is logged and degrades to keyword order. No local model, no required key (a
keyless local server is supported by omitting the auth header).

An answer is used only if it holds one finite, non-empty numeric vector per input,
all of one dimension, in input order (``_checker``). Anything else is malformed:
retried, then the re-rank is skipped. A wrong-typed value used to raise a bare
``TypeError`` out of the search, and a NaN or a shorter vector was ranked as if it
were a real similarity.

NOTE: ``_http`` has no ``json=`` param, so we serialize the JSON ourselves, pass
it as ``content=`` (httpx's non-deprecated raw-body param), and set Content-Type
explicitly.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError
from data_aggregator_mcp.models import DataResource

logger = logging.getLogger(__name__)

_MAX_CHARS = 2000
# Module constants are not mutated: httpx upper-cases the method, and header names are
# case-insensitive, so a case mutant of either is equivalent. test_embeddings_observed
# pins the request each one builds.
_METHOD = "POST"
_JSON_CONTENT = {"Content-Type": "application/json"}
_AUTHORIZATION = "Authorization"
# bool is an int subclass; a JSON true is not a coordinate.
_NUMBER_TYPES = (int, float)
# RFC 6750 §2.1 b64token. Anything else cannot go in the header: httpx raises a bare
# UnicodeEncodeError for a non-ASCII key, and h11's error for a control character
# quotes the header value, key included.
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


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


def _is_vector(v: object) -> bool:
    return (
        isinstance(v, list)
        and len(v) > 0
        and all(type(x) in _NUMBER_TYPES and math.isfinite(x) for x in v)
    )


def _checker(count: int) -> Callable[[Any], None]:
    """A ``request_json`` check for an answer to ``count`` inputs: a ``data`` list of
    ``count`` objects, each with a vector (``_is_vector``) and, if it states one, its
    own position as ``index``, all vectors of one dimension."""

    def check(body: dict) -> None:
        rows = body.get("data")
        if not (
            isinstance(rows, list)
            and len(rows) == count
            and all(
                isinstance(row, dict)
                and row.get("index", i) == i
                and _is_vector(row.get("embedding"))
                for i, row in enumerate(rows)
            )
            and len({len(row["embedding"]) for row in rows}) == 1
        ):
            raise _http.UpstreamEnvelopeError(
                f"no embedding for each of {count} inputs in {body!r:.200}"
            )

    return check


async def embed(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]] | None:
    cfg = _config()
    if cfg is None:
        return None
    base, key, model = cfg
    if key and not _BEARER_TOKEN.fullmatch(key):
        logger.warning("semantic re-rank skipped: EMBEDDING_API_KEY is not a bearer token")
        return None
    headers = dict(_JSON_CONTENT)
    if key:
        headers[_AUTHORIZATION] = f"Bearer {key}"
    payload = json.dumps({"model": model, "input": texts})
    try:
        body = await _http.request_json(
            client,
            _METHOD,
            f"{base}/embeddings",
            service="embeddings",
            content=payload,
            headers=headers,
            expect=dict,
            check=_checker(len(texts)),
        )
    # InvalidURL: an EMBEDDING_API_BASE httpx cannot parse (it is not a TransportError).
    except (DataAggregatorError, httpx.InvalidURL) as exc:
        logger.warning("semantic re-rank skipped: %s", exc)
        return None
    return [row["embedding"] for row in body["data"]]


def cosine_rank(query_vec: list[float], cand_vecs: list[list[float]]) -> list[int]:
    """Indices of candidates sorted by descending cosine similarity to the query
    (ties broken by original order).

    The query norm is a constant across the whole ranking, so it is computed once
    here rather than per candidate — this runs on the search path for every
    ``rank=semantic`` page and every multi-query merge.
    """
    qn = math.sqrt(sum(x * x for x in query_vec))
    if qn == 0.0:
        # Every score would tie at the zero-norm sentinel, so the tiebreak (original
        # order) decides the whole ranking.
        return list(range(len(cand_vecs)))
    scores = []
    for vec in cand_vecs:
        cn = math.sqrt(sum(y * y for y in vec))
        if cn == 0.0:
            scores.append(-1.0)  # the lowest cosine; ties with an opposite vector keep order
            continue
        scores.append(sum(x * y for x, y in zip(query_vec, vec, strict=True)) / (qn * cn))
    return sorted(range(len(cand_vecs)), key=lambda i: (-scores[i], i))


async def rerank(
    client: httpx.AsyncClient, query: str, resources: list[DataResource]
) -> tuple[list[DataResource], str | None]:
    """Re-order ``resources`` by semantic similarity to ``query``. On success
    returns ``(reordered, None)``; if embeddings are unavailable or fail, returns
    ``(resources_unchanged, reason)``."""
    if not resources:
        return resources, None
    texts = [f"{r.title or ''}\n{r.description or ''}"[:_MAX_CHARS] for r in resources]
    # embed's check guarantees one vector per input, all of one dimension.
    vecs = await embed(client, [query, *texts])
    if vecs is None:
        return resources, (
            "semantic re-rank unavailable (no embedding endpoint configured or embed failed)"
        )
    order = cosine_rank(vecs[0], vecs[1:])
    return [resources[i] for i in order], None
