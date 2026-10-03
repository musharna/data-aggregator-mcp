"""Pin the exact merge order, totals, log line and error text of ``_merge``."""

from __future__ import annotations

import logging

import pytest

from data_aggregator_mcp._merge import fan_in, interleave
from data_aggregator_mcp.errors import UpstreamUnavailableError

_LOG = logging.getLogger("tests.merge")


async def _ok(total: int, recs: list[str]) -> tuple[int, list[str]]:
    return total, recs


async def _boom(msg: str) -> tuple[int, list[str]]:
    raise RuntimeError(msg)


def test_interleave_takes_rank_by_rank_across_uneven_lists() -> None:
    assert interleave([["a1"], ["b1", "b2", "b3"], [], ["d1", "d2"]]) == [
        "a1",
        "b1",
        "d1",
        "b2",
        "d2",
        "b3",
    ]
    assert interleave([[], ["only"]]) == ["only"]
    assert interleave([]) == []


async def test_fan_in_sums_totals_and_keeps_backend_order() -> None:
    total, pages = await fan_in(
        {"a": _ok(3, ["a1"]), "b": _ok(0, []), "c": _ok(40, ["c1", "c2"])},
        what="w",
        logger=_LOG,
    )
    assert total == 43
    assert pages == [["a1"], [], ["c1", "c2"]]


async def test_a_partial_outage_is_logged_and_tolerated(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="tests.merge"):
        total, pages = await fan_in(
            {"a": _boom("down"), "b": _ok(2, ["b1"])}, what="lit search", logger=_LOG
        )
    assert (total, pages) == (2, [["b1"]])
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("tests.merge", logging.WARNING, "lit search: a backend failed: RuntimeError('down')")
    ]


async def test_a_total_outage_raises_naming_every_backend() -> None:
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] lit search: every backend failed "
        r"\(a: RuntimeError: x; b: ValueError: y\)$",
    ):
        await fan_in(
            {"a": _boom("x"), "b": _raise(ValueError("y"))}, what="lit search", logger=_LOG
        )
    # positive control: one answering backend is enough
    assert await fan_in({"a": _boom("x"), "b": _ok(0, [])}, what="w", logger=_LOG) == (0, [[]])


async def _raise(exc: Exception) -> tuple[int, list[str]]:
    raise exc


async def test_no_backends_is_an_empty_answer_not_an_outage() -> None:
    assert await fan_in({}, what="w", logger=_LOG) == (0, [])
