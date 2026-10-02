"""Each embeddings answer beside how the re-rank must read it.

Probed 2026-10-02 against a real OpenAI-compatible server (Ollama 0.30.11,
``/v1/embeddings``, model ``all-minilm``; no hosted endpoint is configured here):
- An answer is ``200 {"object": "list", "data": [{"object": "embedding", "embedding":
  [384 floats], "index": i}, ...], "model": ..., "usage": {...}}``, one row per input
  in input order, ``index`` equal to the row's position.
- An empty ``input`` list is ``400 {"error": {"message": "invalid input", ...}}``; an
  unknown model is ``404 {"error": {"message": "model \\"nope\\" not found, ..."}}``.

Before the check, the reader took ``body["data"][i]["embedding"]`` on trust: a string or
null coordinate raised a bare ``TypeError`` out of the search (``router`` calls
``rerank`` unguarded), and a NaN, an infinity, a shorter vector or rows out of order
were ranked and reported as a successful semantic ranking.
"""

import copy
import json
import logging
import math
import os

import httpx
import pytest

from data_aggregator_mcp import _http, embeddings
from data_aggregator_mcp.models import DataResource

# Verbatim from the live server for input ["fruit", "apple\n", "banana\n"], each vector
# trimmed to its first 3 of 384 coordinates. By hand: "fruit"·"apple" < 0 < "fruit"·"banana",
# so banana ranks first.
_ANSWER = {
    "object": "list",
    "data": [
        {
            "object": "embedding",
            "embedding": [-0.0038429908, 0.021971818, -0.028568218],
            "index": 0,
        },
        {"object": "embedding", "embedding": [-0.0062368824, 0.031002397, 0.06488564], "index": 1},
        {"object": "embedding", "embedding": [-0.054388016, 0.013991588, -0.024862582], "index": 2},
    ],
    "model": "all-minilm",
    "usage": {"prompt_tokens": 9, "total_tokens": 9},
}
_REASON = "semantic re-rank unavailable (no embedding endpoint configured or embed failed)"


def _resources() -> list[DataResource]:
    return [
        DataResource(id="a", source="zenodo", kind="dataset", title="apple"),
        DataResource(id="b", source="zenodo", kind="dataset", title="banana"),
    ]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://emb.test/v1")


async def _rerank_answering(text: str) -> tuple[list[str], str | None, int]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text=text, headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out, reason = await embeddings.rerank(client, "fruit", _resources())
    return [r.id for r in out], reason, len(sent)


def _rows(*vectors) -> str:
    return json.dumps({"data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)]})


_MALFORMED = {
    "string vectors": _rows("abc", "def", "ghi"),
    "null coordinate": _rows([1.0, 0.0], [None, 1.0], [0.0, 1.0]),
    "string coordinate": _rows([1.0, 0.0], ["1", 1.0], [0.0, 1.0]),
    "boolean coordinate": _rows([1.0, 0.0], [True, 0.0], [0.0, 1.0]),
    "nested vector": _rows([1.0, 0.0], [[1.0], 0.0], [0.0, 1.0]),
    "NaN coordinate": '{"data": [{"embedding": [1.0, 0.0]}, {"embedding": [NaN, 0.0]},'
    ' {"embedding": [0.0, 1.0]}]}',
    "infinite coordinate": '{"data": [{"embedding": [1.0, 0.0]}, {"embedding": [Infinity, 0.0]},'
    ' {"embedding": [0.0, 1.0]}]}',
    "shorter vector": _rows([1.0, 0.0, 0.0], [1.0], [0.0, 0.0, 1.0]),
    "empty vectors": _rows([], [], []),
    "a row too few": _rows([1.0, 0.0], [0.0, 1.0]),
    "a row too many": _rows([1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]),
    "rows out of order": json.dumps(
        {
            "data": [
                {"embedding": [1.0, 0.0], "index": 0},
                {"embedding": [0.0, 1.0], "index": 2},
                {"embedding": [1.0, 0.0], "index": 1},
            ]
        }
    ),
    "a row not an object": json.dumps(
        {"data": [{"embedding": [1.0, 0.0]}, [1.0, 0.0], {"embedding": [0.0, 1.0]}]}
    ),
    "a row without embedding": json.dumps(
        {"data": [{"embedding": [1.0, 0.0]}, {}, {"embedding": [0.0, 1.0]}]}
    ),
    "no data": json.dumps({"object": "list"}),
    "data not a list": json.dumps({"data": {"embedding": [1.0, 0.0]}}),
    "an error envelope": json.dumps({"error": {"message": "invalid input"}}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", list(_MALFORMED.values()), ids=list(_MALFORMED))
async def test_a_malformed_answer_skips_the_rerank_after_retries(text, caplog):
    # Positive control: the real answer is read and re-ranks.
    assert await _rerank_answering(json.dumps(_ANSWER)) == (["b", "a"], None, 1)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.embeddings"):
        assert await _rerank_answering(text) == (["a", "b"], _REASON, 3)
    [message] = caplog.messages
    assert message.startswith(
        "semantic re-rank skipped: [UpstreamUnavailableError] embeddings returned an "
        "unparseable 200 body after 3 tries: UpstreamEnvelopeError("
    )
    assert "no embedding for each of 3 inputs in {" in message


def _paths(node, prefix=()):
    yield prefix
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*prefix, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _paths(v, (*prefix, i))


def _with(doc, path, value):
    out = copy.deepcopy(doc)
    node = out
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return out


_WRONG = [None, True, 0, 1.5, "x", [], {}, [1.0], {"embedding": [1.0]}]


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Walks every field of the real answer with every JSON type: each must be refused
    by the check or ranked cleanly (the datacite lesson of 2026-10-01)."""
    check = embeddings._checker(3)
    check(_ANSWER)  # positive control: the real answer passes and ranks
    assert embeddings.cosine_rank(
        _ANSWER["data"][0]["embedding"], [r["embedding"] for r in _ANSWER["data"][1:]]
    ) == [1, 0]
    escaped = []
    for path in _paths(_ANSWER):
        if not path:
            continue
        for value in _WRONG:
            body = _with(_ANSWER, path, value)
            try:
                check(body)
            except _http.UpstreamEnvelopeError:
                continue
            vecs = [row["embedding"] for row in body["data"]]
            try:
                order = embeddings.cosine_rank(vecs[0], vecs[1:])
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
                continue
            assert sorted(order) == [0, 1], (path, value)
    assert escaped == []


def test_the_check_names_the_input_count_and_quotes_the_body():
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^no embedding for each of 2 inputs in \{'data': \[\]\}$",
    ):
        embeddings._checker(2)({"data": []})
    embeddings._checker(3)(_ANSWER)  # positive control
    embeddings._checker(1)({"data": [{"embedding": [0.5]}]})  # one coordinate, no index


def test_cosine_rank_refuses_vectors_of_different_dimensions():
    assert embeddings.cosine_rank([1.0, 0.0], [[0.0, 1.0], [1.0, 0.0]]) == [1, 0]
    with pytest.raises(ValueError, match=r"^zip\(\) argument 2 is shorter than argument 1$"):
        embeddings.cosine_rank([1.0, 0.0], [[0.0, 1.0], [1.0]])


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["sk-é", "sk-a\nb", "sk-x ", "Bearer sk-x", "sk=x"])
async def test_a_key_that_is_not_a_bearer_token_is_refused_without_quoting_it(
    key, monkeypatch, caplog
):
    """A non-ASCII key raised a bare UnicodeEncodeError inside httpx, and h11's error for a
    control character quotes the whole header, so logging it would print the key."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Positive control: an RFC 6750 token is sent as the bearer credential.
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-proj-AB_c.d~e+f/g==")
        assert await embeddings.embed(client, ["q"]) == [[1.0]]
        assert sent[0].headers["Authorization"] == "Bearer sk-proj-AB_c.d~e+f/g=="
        monkeypatch.setenv("EMBEDDING_API_KEY", key)
        with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.embeddings"):
            assert await embeddings.embed(client, ["q"]) is None
    assert len(sent) == 1
    assert caplog.messages == ["semantic re-rank skipped: EMBEDDING_API_KEY is not a bearer token"]


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _endpoint_or_skip() -> None:
    if not os.environ.get("EMBEDDING_API_BASE"):
        pytest.skip("no EMBEDDING_API_BASE configured")


@_live_only
@pytest.mark.asyncio
async def test_live_rerank_ranks_the_matching_record_first(live_env):
    """Moved from test_live_p5, where it read EMBEDDING_API_BASE without ``live_env``:
    the autouse fixture had already cleared it, so the test always skipped."""
    _endpoint_or_skip()
    rs = [
        DataResource(
            id="b", source="zenodo", kind="dataset", title="quantum chromodynamics lattice"
        ),
        DataResource(
            id="a", source="zenodo", kind="dataset", title="maize drought tolerance genomics"
        ),
    ]
    async with httpx.AsyncClient() as client:
        out, reason = await embeddings.rerank(client, "corn surviving dry conditions", rs)
    assert reason is None
    assert [r.id for r in out] == ["a", "b"]  # the maize record moves up from second


@_live_only
@pytest.mark.asyncio
async def test_live_answers_pass_the_check(live_env):
    """The check must refuse no real answer: batches of 1 to 20 inputs, including an
    empty string and a long text, each come back as one finite vector per input."""
    _endpoint_or_skip()
    words = ["soil", "moisture", "maize", "", "x" * 2000, "RNA-seq of root tips", "β-catenin"]
    batches = [words[:1], words[:2], words, (words * 3)[:20]]
    async with httpx.AsyncClient() as client:
        for texts in batches:
            vecs = await embeddings.embed(client, texts)
            assert vecs is not None, texts
            assert len(vecs) == len(texts)
            assert len({len(v) for v in vecs}) == 1
            assert all(math.isfinite(x) for v in vecs for x in v)
