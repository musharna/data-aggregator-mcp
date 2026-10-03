"""In-process TTL + LRU cache. Module-level instances persist across tool calls
in the long-lived stdio process. ``ttl <= 0`` disables it (set is a no-op, so get
always misses). ``MISS`` is a sentinel distinct from a stored ``None``."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

MISS = object()


class TTLCache:
    def __init__(
        self,
        maxsize: int,
        ttl: float,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._now = now
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any:
        # No ``ttl <= 0`` check here: ``set`` owns that decision and stores nothing.
        item = self._data.get(key)
        if item is None:
            return MISS
        expires, value = item
        if self._now() >= expires:
            del self._data[key]
            return MISS
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (self._now() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            del self._data[next(iter(self._data))]  # the least recently used

    def clear(self) -> None:
        self._data.clear()
