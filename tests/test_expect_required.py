"""Every adapter reports a 200 whose JSON is the wrong type as an upstream failure.

``_http.request_json``'s ``expect`` was optional until 2026-09-30, and 37 of its 50 call
sites left it out. A caller then read the body with ``(body or {}).get(...)`` or
``body.get(...)``, so a 200 carrying ``null``, ``[]`` or a bare string became an empty
result, "no such record" (dandi, cellxgene), or an untyped ``AttributeError`` (20 entry
points). ``expect`` is now required; this sweep drives each registered adapter's
``search`` and ``resolve`` against a server that answers every request with a JSON
string, and requires the named, retried ``UpstreamUnavailableError``.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, router
from data_aggregator_mcp.errors import UpstreamUnavailableError

# An id each source's resolve accepts as well-formed, so the request is actually sent.
_IDS = {"omicsdi": "omicsdi:pride:PXD000001", "pdb": "pdb:1ABC"}

# Parse JSON outside request_json; the same class of miss, not covered by `expect`.
_OUTSIDE = {("uniprot", "search"): "reads resp.json() after request_with_retry (needs a header)"}

_CASES = [
    (name, fn)
    for name in sorted(router._ADAPTERS)
    for fn in ("search", "resolve")
    if hasattr(router._ADAPTERS[name], fn)
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


async def _call(name: str, fn: str, body: object) -> object:
    mod = router._ADAPTERS[name]
    arg = "x" if fn == "search" else _IDS.get(name, f"{sorted(mod.PREFIXES)[0]}:x1")
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as c:
        return await getattr(mod, fn)(c, arg)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "fn"),
    [
        pytest.param(
            n,
            f,
            marks=pytest.mark.xfail(strict=True, reason=_OUTSIDE[(n, f)]),
        )
        if (n, f) in _OUTSIDE
        else (n, f)
        for n, f in _CASES
    ],
)
async def test_a_wrong_type_200_is_an_upstream_failure(name: str, fn: str) -> None:
    """The failure must be the declared-type check rejecting the string body ("got str"),
    retried and named, not a 404, an AttributeError or any other error the harness could
    provoke. Positive controls for well-formed bodies are each adapter's own tests, which
    now pass through the same required ``expect``; one generic body cannot be
    well-formed here, since endpoints promise an object, a list or (PRIDE) an int."""
    with pytest.raises(UpstreamUnavailableError) as err:
        await _call(name, fn, "junk")
    assert "unparseable 200 body" in str(err.value)
    assert "got str" in str(err.value)
