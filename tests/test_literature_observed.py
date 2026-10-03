"""Pin what ``literature`` sends each backend and how it merges their answers."""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import literature
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _recs(source: str, n: int, desc: str | None = None) -> list[DataResource]:
    return [
        DataResource(
            id=f"{source}:{i}",
            source=source,
            kind="publication",
            title=source,
            description=desc,
            files=[{"name": "f.pdf"}],
        )
        for i in range(n)
    ]


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, str, int, int]]:
    seen: list[tuple[str, str, int, int]] = []

    def backend(name: str, total: int):
        async def search(client, query, *, size, offset):
            seen.append((name, query, size, offset))
            return total, _recs(name, size, desc="x" * 600)

        return search

    monkeypatch.setattr(literature.pubmed, "search", backend("pubmed", 100))
    monkeypatch.setattr(literature.openaire, "search", backend("openaire", 7))
    return seen


async def test_a_subsource_search_defaults_to_the_first_ten(calls) -> None:
    async with httpx.AsyncClient() as client:
        total, recs = await literature.search_subsource(client, "openaire", "q")
    assert calls == [("openaire", "q", 10, 0)]
    assert total == 7 and [r.id for r in recs] == [f"openaire:{i}" for i in range(10)]
    assert all(r.files == [] and len(r.description or "") == 500 for r in recs)  # compact


async def test_a_subsource_search_is_capped_at_fifty(calls) -> None:
    async with httpx.AsyncClient() as client:
        await literature.search_subsource(client, "pubmed", "q", size=50, offset=3)
        await literature.search_subsource(client, "pubmed", "q", size=51, offset=0)
    assert calls == [("pubmed", "q", 50, 3), ("pubmed", "q", 50, 0)]


async def test_search_defaults_to_ten_interleaved_from_both_backends(calls) -> None:
    async with httpx.AsyncClient() as client:
        total, recs = await literature.search(client, "q")
    assert sorted(calls) == [("openaire", "q", 10, 0), ("pubmed", "q", 10, 0)]
    assert total == 107
    assert [r.id for r in recs] == [f"{s}:{i}" for i in range(5) for s in ("pubmed", "openaire")]
    assert all(r.files == [] and len(r.description or "") == 500 for r in recs)


async def test_search_caps_the_page_at_fifty(calls) -> None:
    async with httpx.AsyncClient() as client:
        _, recs = await literature.search(client, "q", size=51, offset=0)
        _, at_cap = await literature.search(client, "q", size=50, offset=0)
    assert {c[2] for c in calls} == {50}
    assert len(recs) == 50 and len(at_cap) == 50


async def test_search_raises_only_when_both_backends_fail(monkeypatch) -> None:
    async def boom(client, query, *, size, offset):
        raise RuntimeError("down")

    monkeypatch.setattr(literature.pubmed, "search", boom)
    monkeypatch.setattr(literature.openaire, "search", boom)
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] literature search: every backend failed "
            r"\(pubmed: RuntimeError: down; openaire: RuntimeError: down\)$",
        ):
            await literature.search(client, "q")


async def test_resolve_hands_the_whole_id_to_the_backend_its_prefix_names(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def backend(name: str):
        async def resolve(client, rid):
            seen.append((name, rid))
            return _recs(name, 1)[0]

        return resolve

    monkeypatch.setattr(literature.pubmed, "resolve", backend("pubmed"))
    monkeypatch.setattr(literature.openaire, "resolve", backend("openaire"))
    async with httpx.AsyncClient() as client:
        await literature.resolve(client, "openaire:doi_dedup___::abc")
        await literature.resolve(client, "pubmed:34320281")
        for bad in ("zenodo:1", "PubMed:1", ":1", "openaire1"):
            with pytest.raises(
                NotFoundError, match=rf"^\[NotFoundError\] unroutable literature id '{bad}'$"
            ):
                await literature.resolve(client, bad)
    assert seen == [("openaire", "openaire:doi_dedup___::abc"), ("pubmed", "pubmed:34320281")]


@live_only
async def test_live_both_backends_answer_a_page_at_the_cap() -> None:
    """The cap is sent at its upper bound: each backend must accept 50 (a cap the
    upstream refuses only shows at the top of the range, as Zenodo's 25 did)."""
    async with httpx.AsyncClient() as client:
        for sub in literature.SUBSOURCES:
            total, recs = await literature.search_subsource(
                client, sub, "arabidopsis auxin", size=50
            )
            assert total > 50 and len(recs) == 50, sub
            assert all(r.id.startswith(f"{sub}:") for r in recs), sub
