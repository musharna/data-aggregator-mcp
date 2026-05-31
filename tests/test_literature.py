from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import literature
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource


def test_prefixes_are_the_two_backends() -> None:
    assert literature.PREFIXES == ("pubmed", "openaire")


async def test_search_interleaves_both_backends(monkeypatch) -> None:
    async def fake_pubmed(client, query, *, size, offset=0):
        return 5, [
            DataResource(id=f"pubmed:{i}", source="pubmed", kind="publication", title="p")
            for i in range(2)
        ]

    async def fake_openaire(client, query, *, size, offset=0):
        return 7, [
            DataResource(id=f"openaire:{i}", source="openaire", kind="publication", title="o")
            for i in range(2)
        ]

    monkeypatch.setattr(literature.pubmed, "search", fake_pubmed)
    monkeypatch.setattr(literature.openaire, "search", fake_openaire)
    async with httpx.AsyncClient() as client:
        total, results = await literature.search(client, "x", size=10)
    assert total == 12
    # round-robin: pubmed:0, openaire:0, pubmed:1, openaire:1
    assert [r.id for r in results] == ["pubmed:0", "openaire:0", "pubmed:1", "openaire:1"]


async def test_search_logs_backend_failure_keeps_partial(monkeypatch, caplog) -> None:
    async def ok(client, query, *, size, offset=0):
        return 1, [DataResource(id="pubmed:1", source="pubmed", kind="publication", title="p")]

    async def boom(client, query, *, size, offset=0):
        raise RuntimeError("openaire down")

    monkeypatch.setattr(literature.pubmed, "search", ok)
    monkeypatch.setattr(literature.openaire, "search", boom)
    import logging

    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient() as client:
            total, results = await literature.search(client, "x")
    assert [r.id for r in results] == ["pubmed:1"]
    assert "openaire" in caplog.text and "down" in caplog.text


async def test_resolve_routes_by_prefix(monkeypatch) -> None:
    async def fake_resolve(client, rid):
        return DataResource(id=rid, source="pubmed", kind="publication", title="p")

    monkeypatch.setattr(literature.pubmed, "resolve", fake_resolve)
    async with httpx.AsyncClient() as client:
        r = await literature.resolve(client, "pubmed:34320281")
    assert r.id == "pubmed:34320281"


@pytest.mark.asyncio
async def test_search_offset_forwarded_to_backends(monkeypatch) -> None:
    seen = []

    async def fake_backend_search(client, query, *, size, offset=0):
        seen.append(offset)
        return 0, []

    for b in literature._BACKENDS.values():
        monkeypatch.setattr(b, "search", fake_backend_search)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        await literature.search(client, "q", size=10, offset=40)
    assert seen and all(o == 40 for o in seen)


async def test_resolve_unroutable_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="unroutable literature id"):
            await literature.resolve(client, "zenodo:1")
