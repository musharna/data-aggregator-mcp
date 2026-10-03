"""Pin the TTL and LRU arithmetic of ``_cache.TTLCache`` exactly, on a fake clock."""

from __future__ import annotations

import pytest

from data_aggregator_mcp._cache import MISS, TTLCache
from tests.test_cache import FakeClock


@pytest.mark.parametrize("ttl", [0.0, -1.0])
def test_a_disabled_cache_stores_nothing(ttl: float) -> None:
    clk = FakeClock()
    c = TTLCache(maxsize=10, ttl=ttl, now=clk.now)
    c.set("k", "v")
    assert c._data == {}  # set is a no-op, not an instantly-expired entry
    assert c.get("k") is MISS
    on = TTLCache(maxsize=10, ttl=0.5, now=clk.now)  # positive control: any ttl > 0 caches
    on.set("k", "v")
    assert on.get("k") == "v"


def test_an_entry_expires_exactly_ttl_after_it_was_set() -> None:
    clk = FakeClock()
    c = TTLCache(maxsize=10, ttl=0.5, now=clk.now)
    clk.t = 10.0
    c.set("k", "v")
    clk.t = 10.4999
    assert c.get("k") == "v"
    clk.t = 10.5
    assert c.get("k") is MISS
    assert "k" not in c._data  # an expired entry is dropped on read
    clk.t = 9.0  # and does not come back
    assert c.get("k") is MISS


def test_setting_again_renews_the_ttl() -> None:
    clk = FakeClock()
    c = TTLCache(maxsize=10, ttl=10.0, now=clk.now)
    c.set("k", "old")
    clk.t = 8.0
    c.set("k", "new")
    clk.t = 15.0
    assert c.get("k") == "new"


def test_a_cache_holds_exactly_maxsize_entries() -> None:
    c = TTLCache(maxsize=3, ttl=100.0)
    for k in "abc":
        c.set(k, k)
    assert [c.get(k) for k in "abc"] == ["a", "b", "c"]
    c.set("d", "d")  # evicts the least recently used: a
    assert list(c._data) == ["b", "c", "d"]


def test_set_and_get_both_make_an_entry_most_recently_used() -> None:
    c = TTLCache(maxsize=2, ttl=100.0)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 10)  # re-set: a becomes MRU, so b is evicted next
    c.set("c", 3)
    assert c.get("b") is MISS and c.get("a") == 10 and c.get("c") == 3
    c.get("a")  # read: a becomes MRU again
    c.set("d", 4)
    assert c.get("c") is MISS and c.get("a") == 10


def test_clear_empties_the_cache() -> None:
    c = TTLCache(maxsize=2, ttl=100.0)
    c.set("a", 1)
    c.clear()
    assert c.get("a") is MISS
    c.set("a", 2)  # still usable
    assert c.get("a") == 2


def test_the_default_clock_is_monotonic() -> None:
    import time

    assert TTLCache(maxsize=1, ttl=1.0)._now is time.monotonic
