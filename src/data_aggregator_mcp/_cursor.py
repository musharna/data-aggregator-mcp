"""Opaque, URL-safe pagination cursor: base64(json(state)). The token is never
promised to callers — only that a ``next_cursor`` from one search is replayable.

The token is not signed, so a client can send any state. ``decode`` refuses every
state a search could not have minted, field by field, before the router reads it: a
wrong type would otherwise escape mid-search as a bare error, and an unbounded
``variants`` list would fan out one request per variant per source."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ValidationError

_REQUIRED = {"q", "size", "offsets"}
# The only filters a search mints (router.search_page builds exactly these three).
_FILTER_KEYS = {"published_after", "published_before", "kind"}


def encode(state: dict[str, Any]) -> str:
    # str.encode/bytes.decode default to UTF-8, and base64 output is ASCII.
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode(token: str) -> dict[str, Any]:
    # The router owns the search domain (its variant cap and kinds) and imports this
    # module, so the limits are read at call time rather than restated here.
    from .router import _VALID_KINDS, MAX_QUERY_VARIANTS

    try:
        # Given a str, b64decode refuses non-ASCII itself (ValueError).
        raw = base64.urlsafe_b64decode(token)
        state = json.loads(raw)
    except Exception as exc:
        raise ValidationError(f"invalid or corrupt cursor: {exc}") from exc
    if not isinstance(state, dict) or not _REQUIRED.issubset(state):
        raise ValidationError("invalid or corrupt cursor: missing required fields")
    # Type validation — a crafted cursor with wrong types can cause fan-out of garbage.
    # 'q' was the one required field whose type went unchecked: a cursor carrying
    # q=null fanned out a None query to every selected source over the network and
    # only died afterwards, inside SearchResult's own validation.
    if not isinstance(state["q"], str):
        raise ValidationError("invalid or corrupt cursor: 'q' must be a string")
    if not isinstance(state["offsets"], dict):
        raise ValidationError("invalid or corrupt cursor: 'offsets' must be a dict")
    if not isinstance(state["size"], int) or isinstance(state["size"], bool) or state["size"] <= 0:
        raise ValidationError("invalid or corrupt cursor: 'size' must be a positive integer")
    if not all(_is_count(v) for v in state["offsets"].values()):
        raise ValidationError("invalid or corrupt cursor: offsets must be non-negative integers")
    ahead = state.get("ahead")
    if ahead is not None and (
        not isinstance(ahead, dict)
        or not all(isinstance(v, list) and all(_is_count(i) for i in v) for v in ahead.values())
    ):
        raise ValidationError(
            "invalid or corrupt cursor: 'ahead' must map streams to non-negative integer lists"
        )
    if "eq" in state and not isinstance(state["eq"], str):
        raise ValidationError("invalid or corrupt cursor: 'eq' must be a string")
    sources = state.get("sources")
    if sources is not None and (
        not isinstance(sources, list) or not all(isinstance(v, str) for v in sources)
    ):
        raise ValidationError("invalid or corrupt cursor: 'sources' must be a list of strings")
    filters = state.get("filters")
    if filters is not None:
        if not isinstance(filters, dict) or not _FILTER_KEYS.issuperset(filters):
            raise ValidationError(
                "invalid or corrupt cursor: 'filters' may hold only "
                "published_after, published_before and kind"
            )
        if not all(_is_year(filters.get(k)) for k in ("published_after", "published_before")):
            raise ValidationError("invalid or corrupt cursor: year filters must be integers")
        kind = filters.get("kind")
        if kind is not None and not (isinstance(kind, str) and kind in _VALID_KINDS):
            raise ValidationError(
                f"invalid or corrupt cursor: 'kind' must be one of {sorted(_VALID_KINDS)}"
            )
    variants = state.get("variants")
    # A search mints 1..MAX_QUERY_VARIANTS variants (the original query is variant 0).
    # Each one is a fan-out to every selected source, so a longer list is a request
    # multiplier no search can ask for; an empty one searches nothing and says so as
    # an empty success.
    if variants is not None and (
        not isinstance(variants, list)
        or not 1 <= len(variants) <= MAX_QUERY_VARIANTS
        or not all(isinstance(v, str) for v in variants)
    ):
        raise ValidationError(
            "invalid or corrupt cursor: 'variants' must be a list of 1 to "
            f"{MAX_QUERY_VARIANTS} strings"
        )
    raw_variants = state.get("raw_variants")
    if raw_variants is not None and (
        not isinstance(raw_variants, list)
        or not all(isinstance(v, str) for v in raw_variants)
        or len(raw_variants) != len(variants or [])
    ):
        raise ValidationError(
            "invalid or corrupt cursor: 'raw_variants' must be strings matching 'variants'"
        )
    return state


def _is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_year(v: object) -> bool:
    """An unset bound or an integer year (``bool`` is an ``int`` to isinstance)."""
    return v is None or (isinstance(v, int) and not isinstance(v, bool))
