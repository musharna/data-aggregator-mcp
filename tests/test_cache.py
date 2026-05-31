from data_aggregator_mcp._cache import MISS, TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def test_hit_within_ttl_then_miss_after_expiry():
    clk = FakeClock()
    c = TTLCache(maxsize=10, ttl=100.0, now=clk.now)
    c.set("k", "v")
    assert c.get("k") == "v"
    clk.t = 99.0
    assert c.get("k") == "v"
    clk.t = 100.0
    assert c.get("k") is MISS  # expired (>= expiry)


def test_lru_eviction_at_maxsize():
    c = TTLCache(maxsize=2, ttl=100.0)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1  # touch a -> a is now MRU
    c.set("c", 3)  # evicts LRU == b
    assert c.get("b") is MISS
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_ttl_zero_disables():
    c = TTLCache(maxsize=10, ttl=0.0)
    c.set("k", "v")
    assert c.get("k") is MISS


def test_stored_none_is_distinct_from_miss():
    c = TTLCache(maxsize=10, ttl=100.0)
    c.set("k", None)
    assert c.get("k") is None
    assert c.get("absent") is MISS
