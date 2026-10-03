"""No adapter lets a resolve id change which endpoint it calls.

cellxgene (#150) built ``{API}/collections/{id}`` from whatever followed the prefix, so
``cellxgene:../collections`` fetched another endpoint. Five more adapters did the same
(zenodo, dandi, gwas, openml, huggingface; probed 2026-09-30). This sweep resolves a
crafted id through every registered adapter and every prefix it owns, against a server
that records each request, and requires that the id never adds a path segment or a
query parameter of its own. Each adapter must also still send a well-formed id, so
one that rejected every id could not pass.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from data_aggregator_mcp import _http, router
from data_aggregator_mcp.errors import DataAggregatorError
from tests._well_formed_ids import WELL_FORMED

# Escapes the record path (``..``) and smuggles a query parameter (``?``).
_CRAFTED = "../../evil?injected=1"

_CASES = [
    (name, prefix)
    for name in sorted(router._ADAPTERS)
    if hasattr(router._ADAPTERS[name], "resolve")
    for prefix in sorted(router._ADAPTERS[name].PREFIXES)
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


def _escaped(url: httpx.URL) -> list[str]:
    """How the crafted id changed this request, if it did."""
    how = []
    if "evil" in url.path.split("/"):
        how.append(f"path {url.path}")
    if "injected" in url.params:
        how.append(f"query {url.query.decode()}")
    return how


async def _resolve(name: str, rid: str) -> list[httpx.URL]:
    sent: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url)
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # every answer is 404, so "not found" (or "malformed") is expected
        with contextlib.suppress(DataAggregatorError):
            await router._ADAPTERS[name].resolve(c, rid)
    return sent


def test_the_detector_sees_an_escape():
    """Positive control for ``_escaped``: a raw path join is flagged, an encoded id
    and a query-parameter id are not."""
    raw = httpx.URL(f"https://api.example/records/{_CRAFTED}")
    assert _escaped(raw) == ["path /evil", "query injected=1"]
    assert _escaped(httpx.URL("https://api.example/records/..%2F..%2Fevil%3Finjected%3D1")) == []
    assert _escaped(httpx.URL("https://api.example/records", params={"id": _CRAFTED})) == []


def _crafted_ids(prefix: str) -> list[str]:
    """The crafted id alone, and in each slot of a multi-part well-formed id: a crafted
    id with the wrong number of parts is refused before any slot is read, so
    `omicsdi:<crafted>` never reached the accession that goes into the path (#88)."""
    parts = WELL_FORMED.get(prefix, "x1").split(":")
    slotted = [[*parts[:i], _CRAFTED, *parts[i + 1 :]] for i in range(len(parts))]
    return [f"{prefix}:{_CRAFTED}"] + [f"{prefix}:{':'.join(p)}" for p in slotted if len(p) > 1]


def test_a_two_part_id_is_crafted_in_each_slot():
    assert _crafted_ids("omicsdi") == [
        f"omicsdi:{_CRAFTED}",
        f"omicsdi:{_CRAFTED}:PXD000001",
        f"omicsdi:pride:{_CRAFTED}",
    ]
    assert _crafted_ids("zenodo") == [f"zenodo:{_CRAFTED}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "prefix"), _CASES)
async def test_a_crafted_resolve_id_cannot_change_the_endpoint(name: str, prefix: str):
    escaped = [
        how
        for rid in _crafted_ids(prefix)
        for url in await _resolve(name, rid)
        for how in _escaped(url)
    ]
    assert escaped == []
    # positive control: a well-formed id still reaches the network
    assert await _resolve(name, f"{prefix}:{WELL_FORMED.get(prefix, 'x1')}") != []
