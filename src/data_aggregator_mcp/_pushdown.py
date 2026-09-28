"""Facet-filter pushdown: turning the router's ``published_after`` / ``published_before``
/ ``kind`` filters into clauses an upstream index evaluates itself.

The router applies these filters after fetch (``router._passes_filters``) to every
stream, and that stays as the correctness net. A post-filter alone, though, sees only
the ``size`` records a stream returned: a page can come back empty while ``total``
still counts the unfiltered upstream. An adapter that can express a filter upstream
implements :class:`FilterPushdown`, so its ``total`` and its windows are the filtered
ones.

The invariant every clause here must keep: the upstream predicate may never exclude a
record the post-filter would keep, or pushdown silently loses results. It holds because
the clause and the normalizer read the same ``kind_map``: a kind is exactly the upstream
types mapped to it, and every unmapped type (image, poster, Other, a missing type, ...)
normalizes to ``"other"`` — the complement of all mapped types.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from data_aggregator_mcp.models import DataResource

YEAR_FILTERS = ("published_after", "published_before")

# The kind a normalizer assigns to any upstream type it does not map. Not "dataset": an
# image or a poster is not a dataset, and must not pass a kind=dataset filter.
OTHER_KIND = "other"


@runtime_checkable
class FilterPushdown(Protocol):
    """An adapter whose upstream can evaluate some facet filters itself."""

    def pushable(self, filters: Mapping[str, Any], /) -> dict[str, Any]:
        """The subset of the ACTIVE (non-None) ``filters`` this upstream can express."""
        ...

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        /,
        *,
        size: int = ...,
        offset: int = ...,
        filters: Mapping[str, Any] | None = ...,
    ) -> tuple[int, list[DataResource]]: ...


def active(filters: Mapping[str, Any]) -> dict[str, Any]:
    """The filters that constrain anything (a None bound or kind is no filter)."""
    return {k: v for k, v in filters.items() if v is not None}


def kind_clause(field: str, kind_map: Mapping[str, str], kind: str) -> str | None:
    """Upstream clause selecting exactly the records ``kind_map`` normalizes to ``kind``,
    or None when no upstream type maps to it (the post-filter then decides). ``other`` is
    every type the map does not name."""
    if kind == OTHER_KIND:
        return f"NOT {field}:({' OR '.join(sorted(kind_map))})" if kind_map else None
    mine = sorted(t for t, k in kind_map.items() if k == kind)
    return f"{field}:({' OR '.join(mine)})" if mine else None


def range_clause(field: str, low: str | None, high: str | None) -> str | None:
    """Inclusive ``field:[low TO high]``, ``*`` for an open end; None when both open."""
    if low is None and high is None:
        return None
    return f"{field}:[{low or '*'} TO {high or '*'}]"


def with_clauses(query: str, clauses: list[str]) -> str:
    """AND the clauses onto the query. The query is parenthesized so a top-level OR in
    it cannot capture a clause; with no clauses the query is returned unchanged. A blank
    query (a filters-only search) contributes no clause: ``()`` is a parse error upstream
    (DataCite answers HTTP 400)."""
    if not clauses:
        return query
    head = [f"({query})"] if query.strip() else []
    return " AND ".join([*head, *clauses])
