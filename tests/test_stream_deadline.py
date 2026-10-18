"""A stream that has not answered by the deadline is named in errors and the page is served.

Timing the round-3 queries (83 searches, every source, 2026-10-17): 4 searches had one
stream stall for 27-56 s (MassIVE, OmicsDI, OpenAIRE), and the page waited for it.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from data_aggregator_mcp import router
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource


def _adapter(source: str, *, stalls: bool) -> types.SimpleNamespace:
    async def search(client, q, *, size=10, offset=0):
        if stalls:
            await asyncio.Event().wait()  # never set: the upstream never answers
        return 2, [
            DataResource(id=f"{source}:{i}", source=source, kind="dataset", title=f"r {i}")
            for i in range(2)
        ]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


async def test_a_stream_past_the_deadline_is_named_and_the_page_is_served(monkeypatch) -> None:
    monkeypatch.setattr(router, "STREAM_DEADLINE_S", 0.05)
    monkeypatch.setattr(
        router,
        "_ADAPTERS",
        {"dataone": _adapter("dataone", stalls=False), "gbif": _adapter("gbif", stalls=True)},
    )
    async with httpx.AsyncClient() as client:
        # Without the deadline the page waits for gbif forever; fail here instead.
        page = await asyncio.wait_for(router.search_page(client, query="snow leopard"), 5)
    assert page.errors["gbif"] == (
        "UpstreamUnavailableError: [UpstreamUnavailableError] no answer within 0.05 s"
    )
    # Positive control: the stream that answered is served, and not named.
    assert [r.id for r in page.results] == ["dataone:0", "dataone:1"]
    assert "dataone" not in page.errors


async def test_a_stream_past_the_deadline_reads_as_a_host_that_did_not_answer(monkeypatch):
    """The nightly live suite skips a test whose host was down, by this flag."""
    monkeypatch.setattr(router, "STREAM_DEADLINE_S", 0.05)
    with pytest.raises(UpstreamUnavailableError) as raised:
        await router._by_deadline(asyncio.Event().wait())
    assert raised.value.down
    assert await router._by_deadline(asyncio.sleep(0, result=7)) == 7  # in time: its answer
