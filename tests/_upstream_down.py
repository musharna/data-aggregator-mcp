"""A live test that failed while an upstream it asked was down is reported skipped.

The nightly live suite asks some 25 hosts, so on most nights one of them is down, and
for weeks every run was red with outages (OSF timeouts, ENA 500s, Zenodo timeouts) that
said nothing about our code: the real breaks it exists for were lost among them. A
failed live test is reported skipped, its reason starting ``upstream down:``, when it
saw a host not answer: an ``UpstreamUnavailableError`` with ``down`` set or a
``RateLimitError`` was raised during it (counted when raised, so one the router or an
adapter caught and turned into an ``errors`` note or a ``None`` counts too), or the
test's own httpx request raised a transport error. Anything else stays a failure, and so does a non-live
test whatever it saw.

With ``DATA_AGGREGATOR_MCP_DOWN_FILE`` set, the skipped tests and their reasons are
written there as JSON for ``scripts/live_verdict.py``, which fails the run when a test
has been down for several runs in a row: a host that is gone for good still turns it red.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import errors

DOWN_FILE_ENV = "DATA_AGGREGATOR_MCP_DOWN_FILE"
_LIVE_ENV = "DATA_AGGREGATOR_MCP_LIVE"
_SEEN = pytest.StashKey[list[str]]()
_DOWN = pytest.StashKey[dict[str, str]]()


def _is_live(item: pytest.Item) -> bool:
    """Every live test carries a skipif whose reason names the variable that runs it."""
    return any(_LIVE_ENV in str(m.kwargs.get("reason", "")) for m in item.iter_markers("skipif"))


@pytest.fixture(autouse=True)
def _record_down_upstreams(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if not _is_live(request.node):
        return
    seen = request.node.stash.setdefault(_SEEN, [])
    unavailable = errors.UpstreamUnavailableError.__init__
    rate_limited = errors.RateLimitError.__init__

    def unavailable_init(self, *args, down=False):
        unavailable(self, *args, down=down)
        if down:
            seen.append(str(self))

    def rate_limited_init(self, *args):
        rate_limited(self, *args)
        seen.append(str(self))

    monkeypatch.setattr(errors.UpstreamUnavailableError, "__init__", unavailable_init)
    monkeypatch.setattr(errors.RateLimitError, "__init__", rate_limited_init)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    if report.failed and call.when in ("setup", "call") and _is_live(item):
        raised = call.excinfo.value if call.excinfo else None
        raw = [repr(raised)] if isinstance(raised, httpx.TransportError) else []
        why = item.stash.get(_SEEN, []) + raw
        if why:
            reason = f"upstream down: {why[0]}"
            report.outcome = "skipped"
            report.longrepr = (str(item.path), item.location[1] + 1, reason)
            item.config.stash.setdefault(_DOWN, {})[item.nodeid] = reason
    return report


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = os.environ.get(DOWN_FILE_ENV)
    if path:
        down = session.config.stash.get(_DOWN, {})
        Path(path).write_text(json.dumps(down, indent=1, sort_keys=True) + "\n")
