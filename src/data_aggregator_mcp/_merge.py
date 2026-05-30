"""Shared fair-merge helper used by the router and multi-db adapters."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def interleave(per_list: list[list[T]]) -> list[T]:
    """Round-robin merge across lists, preserving each list's own order.

    A flat concat + tail truncation would starve later lists whenever an
    earlier one fills the budget; interleaving by rank position gives each a
    fair share.
    """
    out: list[T] = []
    for i in range(max((len(lst) for lst in per_list), default=0)):
        for lst in per_list:
            if i < len(lst):
                out.append(lst[i])
    return out
