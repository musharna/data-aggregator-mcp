"""The request ``embed`` sends, what it logs, and how ``rerank``/``cosine_rank`` order.

Each test pins a value a surviving mutant changed (#88): the endpoint URL built from a
base with a trailing slash, the default model, the exact headers, the service name in
the logged failure, the text embedded per record, the reason string, and the cosine
arithmetic on vectors of different norms.
"""

import json
import logging

import httpx
import pytest

from data_aggregator_mcp import _http, embeddings
from data_aggregator_mcp.models import DataResource

_REASON = "semantic re-rank unavailable (no embedding endpoint configured or embed failed)"


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


def _recording(answer):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request)

    return sent, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _vectors_for(request: httpx.Request) -> httpx.Response:
    n = len(json.loads(request.content)["input"])
    return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}] * n})


@pytest.mark.asyncio
async def test_embed_sends_one_post_to_the_base_without_its_trailing_slash(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1/")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")
    sent, client = _recording(_vectors_for)
    async with client:
        assert await embeddings.embed(client, ["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    [req] = sent
    assert req.method == "POST"
    assert str(req.url) == "https://emb.test/v1/embeddings"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer sk-x"
    # No EMBEDDING_MODEL: the documented default model is asked for.
    assert json.loads(req.content) == {"model": "text-embedding-3-small", "input": ["a", "b"]}


@pytest.mark.asyncio
async def test_embed_without_a_key_sends_no_authorization(monkeypatch):
    # Only the slash is stripped: a base ending in other characters keeps them.
    monkeypatch.setenv("EMBEDDING_API_BASE", "http://127.0.0.1:11434/SANDBOX//")
    monkeypatch.setenv("EMBEDDING_MODEL", "all-minilm")
    sent, client = _recording(_vectors_for)
    async with client:
        assert await embeddings.embed(client, ["a"]) == [[1.0, 0.0]]
    [req] = sent
    assert str(req.url) == "http://127.0.0.1:11434/SANDBOX/embeddings"
    assert "Authorization" not in req.headers
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {"model": "all-minilm", "input": ["a"]}


@pytest.mark.asyncio
async def test_an_error_status_is_logged_naming_the_service(monkeypatch, caplog):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    sent, client = _recording(lambda _r: httpx.Response(400, text="invalid input"))
    async with client:
        with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.embeddings"):
            assert await embeddings.embed(client, ["a"]) is None
    assert len(sent) == 1  # a 400 is not retried
    assert caplog.messages == [
        "semantic re-rank skipped: [UpstreamUnavailableError] embeddings → HTTP 400: invalid input"
    ]


@pytest.mark.asyncio
async def test_a_base_url_httpx_cannot_parse_skips_the_rerank(monkeypatch, caplog):
    sent, client = _recording(_vectors_for)
    async with client:
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://[::1]:8080/v1")  # positive control
        assert await embeddings.embed(client, ["a"]) == [[1.0, 0.0]]
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://[::1/v1")
        with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.embeddings"):
            assert await embeddings.embed(client, ["a"]) is None
    assert len(sent) == 1
    assert caplog.messages == ["semantic re-rank skipped: Invalid port: ':1'"]


@pytest.mark.asyncio
async def test_rerank_embeds_the_query_then_each_title_and_description(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")
    rs = [
        DataResource(id="t", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="d", source="zenodo", kind="dataset", title="", description="a fruit"),
        DataResource(id="n", source="zenodo", kind="dataset", title=""),
        DataResource(id="l", source="zenodo", kind="dataset", title="t", description="d" * 3000),
    ]
    sent, client = _recording(_vectors_for)
    async with client:
        out, reason = await embeddings.rerank(client, "fruit", rs)
    assert reason is None
    assert out == rs  # every vector ties, so the original order stands
    sent_input = json.loads(sent[0].content)["input"]
    assert sent_input[:4] == ["fruit", "apple\n", "\na fruit", "\n"]
    assert sent_input[4] == "t\n" + "d" * 1998  # cut at 2,000 characters
    assert len(sent_input) == 5


@pytest.mark.asyncio
async def test_rerank_without_an_endpoint_names_the_reason():
    rs = [DataResource(id="a", source="zenodo", kind="dataset", title="apple")]
    async with httpx.AsyncClient() as client:
        assert await embeddings.rerank(client, "q", rs) == (rs, _REASON)
        assert await embeddings.rerank(client, "q", []) == ([], None)


def test_cosine_rank_divides_by_both_norms():
    # Unnormalised: the long vector has the larger dot product but the smaller cosine
    # (0.71 against 1.0); multiplying by a norm, or dividing by their ratio, flips it.
    assert embeddings.cosine_rank([2.0, 0.0], [[10.0, 10.0], [1.0, 0.0]]) == [1, 0]
    assert embeddings.cosine_rank([2.0, 0.0], [[1.0, 0.0], [10.0, 10.0]]) == [0, 1]


def test_a_zero_vector_scores_the_lowest_cosine_and_ranks_every_candidate():
    # A zero vector before a real one: every candidate after it still gets its score.
    assert embeddings.cosine_rank([1.0, 0.0], [[0.0, 0.0], [1.0, 0.0]]) == [1, 0]
    # It scores -1, the lowest cosine, so it ties with an opposite vector and the tie
    # keeps the original order, either way round.
    assert embeddings.cosine_rank([1.0, 0.0], [[0.0, 0.0], [-1.0, 0.0]]) == [0, 1]
    assert embeddings.cosine_rank([1.0, 0.0], [[-1.0, 0.0], [0.0, 0.0]]) == [0, 1]
