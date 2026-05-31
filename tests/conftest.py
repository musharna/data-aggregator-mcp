"""Shared pytest fixtures for data-aggregator-mcp."""

from __future__ import annotations

import pytest

from data_aggregator_mcp import _ratelimit, router, taxonomy


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """Reset module-level process-lifetime state between tests.

    The rate-limiter buckets and the resolve cache live at module scope so they
    persist across tool calls in the long-lived stdio server (one event loop for
    the process). pytest-asyncio gives each test a *fresh* event loop, so a
    bucket created in one test's now-closed loop would misfire if reused by a
    later test. Clearing both before each test restores the one-loop assumption
    the production code is built on. (test_ratelimit.py also resets locally;
    this lifts that to every test file.)
    """
    _ratelimit.reset()
    router._RESOLVE_CACHE.clear()
    taxonomy._CACHE.clear()
    yield
    _ratelimit.reset()
    router._RESOLVE_CACHE.clear()
    taxonomy._CACHE.clear()
