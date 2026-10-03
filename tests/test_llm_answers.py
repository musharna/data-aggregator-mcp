"""Each chat-completions answer and failure beside how ``llm.complete_json`` must read it.

Probed 2026-10-02 against a real OpenAI-compatible server (Ollama 0.30.11,
``/v1/chat/completions``, models ``llama3.1`` and ``qwen3:14b``, CPU/GPU-local; no hosted
endpoint is configured here):
- An answer is ``200 {"id", "object": "chat.completion", "created", "model",
  "system_fingerprint", "choices": [{"index": 0, "message": {"role": "assistant",
  "content": "<JSON text>"}, "finish_reason": "stop"}], "usage": {...}}``. qwen3 adds the
  model's reasoning as ``message.reasoning``; ``content`` is still bare JSON, with or
  without ``response_format``.
- An unknown model is ``404 {"error": {"message": "model 'nope-model' not found",
  "type": "not_found_error", ...}}``; a malformed request is ``400`` with the same envelope.

The contract is that nothing raises into the search path. Before this file, every
failure also left no trace (a broad ``except Exception`` returned None), an answer without
a message was not retried, and a line-broken ``LLM_API_KEY`` (a CRLF ``.env``) was an h11
protocol error retried for ~3 s per call, whose text quotes the header, key included.
"""

from __future__ import annotations

import copy
import http.server
import json
import logging
import os
import threading
from collections.abc import Iterator

import httpx
import pytest

from data_aggregator_mcp import _http, llm

# Verbatim from Ollama (llama3.1) for the probe prompt above.
_LLAMA = {
    "id": "chatcmpl-770",
    "object": "chat.completion",
    "created": 1790975380,
    "model": "llama3.1",
    "system_fingerprint": "fp_ollama",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": '{\n"keyword_core": "rna-seq maize roots drought",\n'
                '"organism": "maize",\n"year_min": "2019",\n"year_max": null\n}',
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 53, "completion_tokens": 37, "total_tokens": 90},
}
_LLAMA_OBJECT = {
    "keyword_core": "rna-seq maize roots drought",
    "organism": "maize",
    "year_min": "2019",
    "year_max": None,
}
# Verbatim from Ollama (qwen3:14b), the reasoning trimmed to its first sentence.
_QWEN = {
    "id": "chatcmpl-940",
    "object": "chat.completion",
    "created": 1790975399,
    "model": "qwen3:14b",
    "system_fingerprint": "fp_ollama",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": '{\n  "keyword_core": "RNA-seq of roots under drought",\n'
                '  "organism": "maize",\n  "year_min": 2019,\n  "year_max": null\n}',
                "reasoning": "Okay, let's see.",
            },
            "finish_reason": "stop",
        }
    ],
}
_UNKNOWN_MODEL = {
    "error": {
        "message": "model 'nope-model' not found",
        "type": "not_found_error",
        "param": None,
        "code": None,
    }
}
_BASE = "https://llm.test/v1"
_SKIPPED = "query understanding skipped: "


@pytest.fixture(autouse=True)
def configured(monkeypatch) -> list[float]:
    waits: list[float] = []

    async def _no_sleep(seconds, *_a, **_k):
        waits.append(seconds)

    async def _no_wait(*_a):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    # The per-host rate (10/s) is not under test here, and the field walk sends ~250 requests.
    monkeypatch.setattr(_http._ratelimit, "acquire", _no_wait)
    monkeypatch.setenv("LLM_API_BASE", _BASE)
    return waits


async def _ask(handler) -> tuple[dict | None, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        out = await llm.complete_json(client, system="s", user="u")
    return out, sent


def _answering(status: int, text: str):
    return lambda _r: httpx.Response(
        status, text=text, headers={"content-type": "application/json"}
    )


def _ok(body: object):
    return _answering(200, json.dumps(body))


async def _reads_the_real_answer() -> None:
    out, sent = await _ask(_ok(_LLAMA))
    assert out == _LLAMA_OBJECT
    assert len(sent) == 1


async def test_real_answers_are_read_as_their_content_object(caplog):
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        await _reads_the_real_answer()
        out, _ = await _ask(_ok(_QWEN))
    assert out == {
        "keyword_core": "RNA-seq of roots under drought",
        "organism": "maize",
        "year_min": 2019,
        "year_max": None,
    }
    assert caplog.messages == []


async def test_the_first_choice_is_read():
    second = copy.deepcopy(_LLAMA["choices"][0])
    second["message"]["content"] = '{"keyword_core": "second"}'
    body = copy.deepcopy(_LLAMA)
    body["choices"].append(second)
    out, _ = await _ask(_ok(body))
    assert out == _LLAMA_OBJECT


def _choice(message: object) -> dict:
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


# Answers without a message in their first choice: the server's fault, retried.
_NO_MESSAGE = {
    "no choices": {"object": "chat.completion"},
    "choices empty": {"choices": []},
    "choices an object": {"choices": {"0": _LLAMA["choices"][0]}},
    "choices a string": {"choices": "abc"},
    "choices null": {"choices": None},
    "a choice not an object": {"choices": ["abc"]},
    "a choice null": {"choices": [None]},
    "no message": {"choices": [{"index": 0, "finish_reason": "stop"}]},
    "message null": _choice(None),
    "message a string": _choice('{"keyword_core": "x"}'),
    "message a list": _choice([{"content": "{}"}]),
    "an error envelope": _UNKNOWN_MODEL,
}


@pytest.mark.parametrize("body", list(_NO_MESSAGE.values()), ids=list(_NO_MESSAGE))
async def test_an_answer_without_a_message_is_retried_then_skipped(body, caplog):
    await _reads_the_real_answer()  # positive control
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_ok(body))
    assert out is None
    assert len(sent) == 3
    assert caplog.messages == [
        f"{_SKIPPED}[UpstreamUnavailableError] llm returned an unparseable 200 body after 3 "
        f"tries: UpstreamEnvelopeError({f'no chat message in {body!r:.200}'!r})"
    ]


@pytest.mark.parametrize("text", ["[]", "null", '"choices"', "not json {"])
async def test_a_body_that_is_not_an_object_is_retried_then_skipped(text, caplog):
    await _reads_the_real_answer()  # positive control
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_answering(200, text))
    assert out is None
    assert len(sent) == 3
    [message] = caplog.messages
    assert message.startswith(
        f"{_SKIPPED}[UpstreamUnavailableError] llm returned an unparseable 200 body after 3 tries: "
    )


# Message content that is not a JSON object: the model's answer, not retried (the same
# prompt at temperature 0 gets the same answer).
_NOT_TEXT = {"null": None, "a number": 1, "an object": {"keyword_core": "x"}, "a list": []}
_NOT_AN_OBJECT = {
    "empty": "",
    "prose": "Sure! Here is the JSON you asked for.",
    "cut short": '{"keyword_core": "rna',
    "a list": "[1, 2, 3]",
    "a string": '"rna"',
    "a number": "2019",
    "null": "null",
    "a fenced object": '```json\n{"keyword_core": "rna"}\n```',
    "nested too deep": "[" * 100_000 + "]" * 100_000,
}


@pytest.mark.parametrize("content", list(_NOT_TEXT.values()), ids=list(_NOT_TEXT))
async def test_message_content_that_is_not_text_is_skipped_once(content, caplog):
    await _reads_the_real_answer()  # positive control
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_ok(_choice({"role": "assistant", "content": content})))
    assert out is None
    assert len(sent) == 1
    assert caplog.messages == [f"{_SKIPPED}the message content is not text: {content!r:.200}"]


async def test_a_message_without_content_is_skipped_once(caplog):
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_ok(_choice({"role": "assistant"})))
    assert (out, len(sent)) == (None, 1)
    assert caplog.messages == [f"{_SKIPPED}the message content is not text: None"]
    await _reads_the_real_answer()  # positive control


@pytest.mark.parametrize("content", list(_NOT_AN_OBJECT.values()), ids=list(_NOT_AN_OBJECT))
async def test_message_content_that_is_not_a_json_object_is_skipped_once(content, caplog):
    await _reads_the_real_answer()  # positive control
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_ok(_choice({"role": "assistant", "content": content})))
    assert out is None
    assert len(sent) == 1
    assert caplog.messages == [
        f"{_SKIPPED}the message content is not a JSON object: {content!r:.200}"
    ]


_ERRORS = {
    "unknown model (Ollama, verbatim)": (
        404,
        _UNKNOWN_MODEL,
        1,
        f"[NotFoundError] llm → HTTP 404: {json.dumps(_UNKNOWN_MODEL)}",
    ),
    "bad request": (
        400,
        {"error": "bad"},
        1,
        '[UpstreamUnavailableError] llm → HTTP 400: {"error": "bad"}',
    ),
    "unauthorized": (
        401,
        {"error": "no key"},
        1,
        '[UpstreamUnavailableError] llm → HTTP 401: {"error": "no key"}',
    ),
    "server error": (
        500,
        {"error": "boom"},
        3,
        "[UpstreamUnavailableError] llm exhausted 3 retries (last HTTP 500)",
    ),
    "rate limited": (
        429,
        {"error": "slow"},
        3,
        "[RateLimitError] llm exhausted 3 retries (HTTP 429)",
    ),
}


@pytest.mark.parametrize(
    ("status", "body", "tries", "reason"), list(_ERRORS.values()), ids=list(_ERRORS)
)
async def test_an_error_status_is_skipped_and_logged(status, body, tries, reason, caplog):
    await _reads_the_real_answer()  # positive control
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_answering(status, json.dumps(body)))
    assert out is None
    assert len(sent) == tries
    assert caplog.messages == [_SKIPPED + reason]


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["refused", "timeout"]
)
async def test_an_unreachable_endpoint_is_skipped_and_logged(exc, caplog):
    await _reads_the_real_answer()  # positive control

    def fail(_r):
        raise exc

    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(fail)
    assert (out, len(sent)) == (None, 3)
    assert caplog.messages == [
        f"{_SKIPPED}[UpstreamUnavailableError] llm unreachable after 3 tries: {exc!r}"
    ]


@pytest.mark.parametrize(
    "key", ["sk-é", "sk-a\nb", "sk-x\r", "sk-x ", "Bearer sk-x", "sk=x", "sk x"]
)
async def test_a_key_that_is_not_a_bearer_token_is_refused_without_quoting_it(
    key, monkeypatch, caplog
):
    # Positive control: an RFC 6750 token is sent as the bearer credential.
    monkeypatch.setenv("LLM_API_KEY", "sk-proj-AB_c.d~e+f/g==")
    out, sent = await _ask(_ok(_LLAMA))
    assert out == _LLAMA_OBJECT
    assert sent[0].headers["Authorization"] == "Bearer sk-proj-AB_c.d~e+f/g=="
    monkeypatch.setenv("LLM_API_KEY", key)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, sent = await _ask(_ok(_LLAMA))
    assert (out, sent) == (None, [])
    assert caplog.messages == [f"{_SKIPPED}LLM_API_KEY is not a bearer token"]


class _Chat(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers["content-length"]))
        body = json.dumps(_LLAMA).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture
def local_chat_server() -> Iterator[str]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Chat)
    thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/v1"
    finally:
        srv.shutdown()
        srv.server_close()


async def test_a_line_broken_key_never_reaches_the_wire(
    local_chat_server, configured, monkeypatch, caplog
):
    """Over a real socket (MockTransport skips h11): a key ending in ``\\r``, as a CRLF
    ``.env`` leaves it, used to be an h11 ``LocalProtocolError``, which ``_http`` retries as
    a network failure (two backoff waits per call) and whose text quotes the header."""
    monkeypatch.setenv("LLM_API_BASE", local_chat_server)
    monkeypatch.setenv("LLM_API_KEY", "sk-local")
    async with httpx.AsyncClient() as client:
        assert await llm.complete_json(client, system="s", user="u") == _LLAMA_OBJECT
        monkeypatch.setenv("LLM_API_KEY", "sk-local\r")
        with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
            assert await llm.complete_json(client, system="s", user="u") is None
    assert configured == []  # no retry backoff
    assert caplog.messages == [f"{_SKIPPED}LLM_API_KEY is not a bearer token"]


async def test_an_error_body_echoing_the_key_is_logged_without_it(monkeypatch, caplog):
    key = "sk-proj-SECRET123"
    monkeypatch.setenv("LLM_API_KEY", key)
    await _reads_the_real_answer()  # positive control: the key is valid and sent
    echo = {"error": {"message": f"Incorrect API key provided: {key}"}}
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        out, _ = await _ask(_answering(401, json.dumps(echo)))
    assert out is None
    assert caplog.messages == [
        f"{_SKIPPED}[UpstreamUnavailableError] llm → HTTP 401: "
        '{"error": {"message": "Incorrect API key provided: <LLM_API_KEY>"}}'
    ]


@pytest.mark.parametrize(
    "base",
    [
        "localhost:11434/v1",  # no scheme
        "http://",
        "http:///v1",
        "ftp://llm.test/v1",
        "file:///etc/passwd",
        "http://[::1",  # httpx: InvalidURL
        "http://a\tb/v1",  # httpx: InvalidURL
        "http://xn--/v1",  # idna: IDNAError, a ValueError
        "http://xn--a/v1",  # idna: InvalidCodepoint
    ],
)
async def test_a_base_that_is_not_an_http_url_is_refused_before_the_network(
    base, monkeypatch, caplog
):
    await _reads_the_real_answer()  # positive control: https://llm.test/v1
    monkeypatch.setenv("LLM_API_BASE", base)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        assert await _ask(_ok(_LLAMA)) == (None, [])
    assert caplog.messages == [f"{_SKIPPED}LLM_API_BASE is not an http(s) URL"]


async def test_an_upper_case_scheme_is_an_http_url(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "HTTPS://llm.test/v1")
    out, sent = await _ask(_ok(_LLAMA))
    assert out == _LLAMA_OBJECT
    assert str(sent[0].url) == "https://llm.test/v1/chat/completions"


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


_WRONG = [None, True, 0, 1.5, "x", "{}", [], [{}], {}, {"content": "{}"}]


async def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Walks every field of the real answer with every JSON type: each must come back as
    None or a dict, never raise (the datacite lesson of 2026-10-01)."""
    await _reads_the_real_answer()  # positive control
    escaped = []
    for path in _paths(_QWEN):
        if not path:
            continue
        for value in _WRONG:
            try:
                out, _ = await _ask(_ok(_with(_QWEN, path, value)))
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
                continue
            assert out is None or isinstance(out, dict), (path, value)
    assert escaped == []


def test_the_check_quotes_the_answer():
    with pytest.raises(
        _http.UpstreamEnvelopeError, match=r"^no chat message in \{'choices': \[\]\}$"
    ):
        llm._check_answer({"choices": []})
    llm._check_answer(_LLAMA)  # positive control
    llm._check_answer(_choice({}))  # a message object is enough; its content is read after


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _endpoint_or_skip(real_env: dict[str, str]) -> None:
    # Asks the operator's environment, not os.environ: the autouse fixture above has
    # set a mock endpoint, which live_env overrides only when a real one exists.
    if not real_env.get("LLM_API_BASE"):
        pytest.skip("no LLM_API_BASE configured")


@_live_only
async def test_live_an_answer_passes_the_check_and_is_read(live_env, caplog):
    _endpoint_or_skip(live_env)
    system = (
        "Extract a search query as JSON with keys keyword_core, organism, year_min, "
        "year_max. Answer with a JSON object only."
    )
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        async with httpx.AsyncClient() as client:
            out = await llm.complete_json(
                client, system=system, user="RNA-seq of maize roots under drought since 2019"
            )
    assert caplog.messages == []
    assert isinstance(out, dict)
    assert "keyword_core" in out


@_live_only
async def test_live_an_unknown_model_is_skipped_and_logged(live_env, monkeypatch, caplog):
    _endpoint_or_skip(live_env)
    monkeypatch.setenv("LLM_MODEL", "no-such-model-data-aggregator-mcp")
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        async with httpx.AsyncClient() as client:
            assert await llm.complete_json(client, system="s", user="u") is None
    [message] = caplog.messages
    assert message.startswith(f"{_SKIPPED}[")
    key = live_env.get("LLM_API_KEY")
    assert not key or key not in message
