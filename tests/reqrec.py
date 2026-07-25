# tests/reqrec.py
"""Opt-in pytest plugin: record every request that reaches a REAL httpx transport.

It answers one question mechanically — *which upstreams does the suite never
actually contact?* An endpoint that appears only under `pytest-httpx` mocks is an
endpoint whose request format has never been checked against the server that has
to accept it, because the mock's matcher and the code's format come from the same
assumption. Nothing in such a test can fail when that assumption is wrong.

That blind spot is not hypothetical. `fulltext.py` asked EuropePMC for a
phrase-quoted `PMCID:"PMC3463246"` for months; EuropePMC returns zero hits for
the quoted form, so every OA paper we knew a PMCID for looked paywalled. Six
mocked tests covered the path and all six passed — one of them asserted the
quoting *was* correct. A run of this plugin over the live suite is what showed
`api.unpaywall.org`, the sibling leg in the same module, was likewise never
contacted.

Usage (nothing is patched unless `-p tests.reqrec` is passed; `PYTHONPATH=.` is
required because plugin import happens before pytest puts the rootdir on the
path)::

    PYTHONPATH=. DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -p tests.reqrec -q
    REQREC_OUT=hosts.txt PYTHONPATH=. DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -p tests.reqrec -q

Endpoints go to `REQREC_OUT` (default `reqrec.txt`) with a per-host rollup on the
terminal. Compare that against the upstreams the code can reach; the difference
is the mock-only set, and each entry there wants one live test.

**The hook point is the whole point of this file.** `pytest-httpx` installs its
mock *as the transport*, so it sits BELOW `httpx.AsyncClient.send` and ABOVE
`httpx.AsyncHTTPTransport.handle_async_request`. Patching the client therefore
records mocked traffic as though it were real and reports false coverage — the
throwaway first version of this recorder did exactly that and wrongly cleared
Unpaywall as "covered". Patching the transport records only what left the
machine. Instrumentation placed above the mock layer measures the mocks.

The difference is reproducible, not theoretical: over `tests/test_fulltext.py`
with no live gate (so every request is mocked and nothing touches the network), a
client-layer recorder reports four endpoints — three of them `api.unpaywall.org`
— while this transport-layer one correctly reports none.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

import httpx

DEFAULT_OUT = "reqrec.txt"

_seen: Counter[str] = Counter()
_originals: dict[str, Any] = {}


# The hooks below deliberately declare no parameters they do not use: pytest passes a
# hook implementation only the arguments it actually names.
def pytest_configure() -> None:
    async_orig = httpx.AsyncHTTPTransport.handle_async_request
    sync_orig = httpx.HTTPTransport.handle_request
    _originals["async"] = async_orig
    _originals["sync"] = sync_orig

    async def handle_async_request(self, request: httpx.Request, *a: Any, **kw: Any):
        _seen[f"{request.url.host}{request.url.path}"] += 1
        return await async_orig(self, request, *a, **kw)

    def handle_request(self, request: httpx.Request, *a: Any, **kw: Any):
        _seen[f"{request.url.host}{request.url.path}"] += 1
        return sync_orig(self, request, *a, **kw)

    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request
    httpx.HTTPTransport.handle_request = handle_request


def pytest_unconfigure() -> None:
    if _originals:
        httpx.AsyncHTTPTransport.handle_async_request = _originals.pop("async")
        httpx.HTTPTransport.handle_request = _originals.pop("sync")


def pytest_terminal_summary(terminalreporter: Any) -> None:
    out = os.environ.get("REQREC_OUT", DEFAULT_OUT)
    hosts: Counter[str] = Counter()
    for endpoint, n in _seen.items():
        hosts[endpoint.split("/", 1)[0]] += n
    lines = [f"{n:>6}  {endpoint}" for endpoint, n in sorted(_seen.items())]
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    terminalreporter.write_sep("-", "real outbound requests (below the mock transport)")
    if not hosts:
        # Zero is the expected result for a default (mocked) run: it means nothing
        # escaped the mocks, NOT that the recorder is broken. Only a live run
        # (DATA_AGGREGATOR_MCP_LIVE=1) exercises real upstreams.
        terminalreporter.write_line("none — no request reached a real transport")
        return
    for host, n in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0])):
        terminalreporter.write_line(f"{n:>6}  {host}")
    terminalreporter.write_line(f"({len(_seen)} distinct endpoints written to {out})")
