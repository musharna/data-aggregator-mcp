"""Shared pytest fixtures for data-aggregator-mcp."""

from __future__ import annotations

import os

import pytest

from data_aggregator_mcp import _ratelimit, router, taxonomy, zenodo

# Every environment variable the server reads as configuration. A test's behavior must
# not depend on which of these happen to be exported in the shell that runs it: with
# UNPAYWALL_EMAIL set, `fulltext.find()` takes the Unpaywall leg, and a test that mocked
# only EuropePMC then dies on an unregistered request. CI has never caught that class
# because CI's environment is empty — the suite was only ever hermetic by accident.
_APP_ENV_VARS = (
    "CACHE_TTL_SECONDS",
    "DATAVERSE_BASE_URL",
    "DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS",
    "DATA_GOV_API_KEY",
    "EMBEDDING_API_BASE",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "LLM_API_BASE",
    "LLM_API_KEY",
    "LLM_MODEL",
    "NCBI_API_KEY",
    "NCBI_EMAIL",
    "UNPAYWALL_EMAIL",
)

# Captured at import, before any fixture clears them, because the live tests need the
# real values back. DATA_AGGREGATOR_MCP_LIVE is deliberately absent: it selects which
# tests run, it is not server configuration.
_REAL_ENV = {name: os.environ[name] for name in _APP_ENV_VARS if name in os.environ}


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
    zenodo._SEARCH_CACHE.clear()  # search-seeded record cache (resolve double-fetch skip)
    yield
    _ratelimit.reset()
    router._RESOLVE_CACHE.clear()
    taxonomy._CACHE.clear()
    zenodo._SEARCH_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_app_env(monkeypatch):
    """Run every test against an empty server configuration.

    A test that wants a variable set says so with ``monkeypatch.setenv`` — which still
    wins, since it runs inside the test body, after this fixture. Live tests that need
    the operator's real credentials take the ``live_env`` fixture below.
    """
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def live_env(_isolate_app_env, monkeypatch):
    """Opt back in to the real environment captured at import.

    Live tests talk to real upstreams, which want the identifiers and keys those
    upstreams require. Depends on ``_isolate_app_env`` explicitly so the restore is
    ordered after the clear rather than relying on fixture-ordering luck.
    """
    for name, value in _REAL_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(_REAL_ENV)
