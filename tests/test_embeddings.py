import json

import httpx
import pytest

from data_aggregator_mcp import embeddings
from data_aggregator_mcp.models import DataResource


def test_cosine_rank_orders_by_similarity_zero_norm_last():
    q = [1.0, 0.0]
    cands = [[0.0, 1.0], [1.0, 0.0], [0.0, 0.0]]  # orthogonal, identical, zero
    assert embeddings.cosine_rank(q, cands) == [1, 0, 2]


@pytest.mark.asyncio
async def test_embed_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    async with httpx.AsyncClient() as client:
        assert await embeddings.embed(client, ["a", "b"]) is None


@pytest.mark.asyncio
async def test_embed_posts_and_parses(httpx_mock, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    httpx_mock.add_response(
        url="https://emb.test/v1/embeddings",
        json={"data": [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]}]},
    )
    async with httpx.AsyncClient() as client:
        vecs = await embeddings.embed(client, ["a", "b"])
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]
    req = httpx_mock.get_requests()[0]
    assert req.headers["authorization"] == "Bearer sk-x"
    assert json.loads(req.content) == {"model": "m", "input": ["a", "b"]}


@pytest.mark.asyncio
async def test_rerank_unconfigured_returns_unchanged_with_reason(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    rs = [
        DataResource(id="a", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="b", source="zenodo", kind="dataset", title="banana"),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "fruit", rs)
    assert out == rs
    assert reason is not None


@pytest.mark.asyncio
async def test_rerank_reorders_on_success(httpx_mock, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    httpx_mock.add_response(
        url="https://emb.test/v1/embeddings",
        json={
            "data": [
                {"embedding": [0.0, 1.0]},  # query
                {"embedding": [1.0, 0.0]},  # cand 0 (orthogonal)
                {"embedding": [0.0, 1.0]},  # cand 1 (identical to query)
            ]
        },
    )
    rs = [
        DataResource(id="a", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="b", source="zenodo", kind="dataset", title="banana"),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "q", rs)
    assert reason is None
    assert [r.id for r in out] == ["b", "a"]
