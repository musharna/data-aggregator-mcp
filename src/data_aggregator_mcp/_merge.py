"""Shared fair-merge helper used by the router and multi-db adapters."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable
from typing import TypeVar, cast

from data_aggregator_mcp.errors import UpstreamUnavailableError

T = TypeVar("T")
_GAP = object()  # fills the shorter lists in a rank; never in the output


def interleave(per_list: list[list[T]]) -> list[T]:
    """Round-robin merge across lists, preserving each list's own order.

    A flat concat + tail truncation would starve later lists whenever an
    earlier one fills the budget; interleaving by rank position gives each a
    fair share.
    """
    ranks = itertools.zip_longest(*per_list, fillvalue=_GAP)
    return cast(list[T], [x for x in itertools.chain.from_iterable(ranks) if x is not _GAP])


async def fan_in(
    calls: dict[str, Awaitable[tuple[int, list[T]]]], *, what: str, logger: logging.Logger
) -> tuple[int, list[list[T]]]:
    """Await one search per backend of a composite adapter; return ``(summed_total,
    per-backend pages)`` for the backends that answered.

    A partial outage is logged and tolerated (the others still answer), but a TOTAL
    one raises ``UpstreamUnavailableError``: returning ``(0, [])`` there made "every
    NCBI database is down" read as "no data exists". The router does not use this
    path — it pages each backend as its own stream so a failure lands in ``errors``
    by name — but the adapter's own ``search`` keeps the contract for direct callers.
    """
    names = list(calls)
    outcomes = await asyncio.gather(*calls.values(), return_exceptions=True)
    total = 0
    pages: list[list[T]] = []
    failures: list[str] = []
    # gather() returns one outcome per awaitable, so strict= cannot change behaviour.
    for name, outcome in zip(names, outcomes, strict=True):  # pragma: no mutate
        if isinstance(outcome, BaseException):
            logger.warning("%s: %s backend failed: %r", what, name, outcome)
            failures.append(f"{name}: {type(outcome).__name__}: {outcome}")
            continue
        backend_total, recs = outcome
        total += backend_total
        pages.append(recs)
    if names and len(failures) == len(names):
        raise UpstreamUnavailableError(f"{what}: every backend failed ({'; '.join(failures)})")
    return total, pages
