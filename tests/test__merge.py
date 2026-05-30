from __future__ import annotations

from data_aggregator_mcp._merge import interleave


def test_interleave_round_robin_preserves_per_list_order() -> None:
    assert interleave([[1, 3, 5], [2, 4]]) == [1, 2, 3, 4, 5]


def test_interleave_empty() -> None:
    assert interleave([]) == []
    assert interleave([[], []]) == []
