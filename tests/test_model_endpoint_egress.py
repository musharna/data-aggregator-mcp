"""The egress guard and the model endpoints the operator configures.

``LLM_API_BASE`` and ``EMBEDDING_API_BASE`` are operator configuration, and a local model
server (Ollama on 127.0.0.1:11434) is their common case. They share the server's HTTP
client with record URLs, which are attacker-controlled, and that client checks every hop
against the private-address guard. Until 0.55.1 the guard refused a local model server
like any record URL, so query understanding and semantic re-rank were off unless the
operator switched the guard off for everything.

These tests run with the guard ON, over real sockets, through ``server._http_client``.
Each one also asserts what must stay refused through the same client: a record-style
request to the very same private origin, and a redirect out of the configured origin.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from collections.abc import Iterator

import httpx
import pytest

from data_aggregator_mcp import egress, embeddings, llm, server
from data_aggregator_mcp.errors import ValidationError
from tests.test_llm_answers import _LLAMA, _LLAMA_OBJECT

_ALLOW = "DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS"
_REFUSED = r"^\[ValidationError\] request: '127\.0\.0\.1' resolves to the non-public address "


class _Model(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible model server: chat and embeddings by POST, and a GET that
    stands for anything else listening there which a record must not reach."""

    hits: list[str]

    def do_POST(self):  # noqa: N802 - http.server's name
        self.hits.append(f"POST {self.path}")
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path.endswith("/chat/completions"):
            answer = _LLAMA
        else:
            answer = {
                "data": [
                    {"index": i, "embedding": [1.0, float(i)]} for i in range(len(body["input"]))
                ]
            }
        self._send(200, json.dumps(answer).encode())

    def do_GET(self):  # noqa: N802
        self.hits.append(f"GET {self.path}")
        self._send(200, b"internal")

    def _send(self, status: int, body: bytes, location: str | None = None) -> None:
        self.send_response(status)
        if location:
            self.send_header("location", location)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def model() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []
    handler = type("Model", (_Model,), {"hits": hits})
    for origin in _serve(handler):
        yield origin, hits


@pytest.fixture
def redirector(model) -> Iterator[str]:
    """A model base that answers every POST with a 307 to the model server, a different
    private origin (another port) the operator did not configure."""
    target, _ = model

    class _Redirect(_Model):
        hits: list[str] = []  # noqa: RUF012 - unused, the target records the hits

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["content-length"]))
            self._send(307, b"", location=f"{target}{self.path}")

    yield from _serve(_Redirect)


@pytest.fixture
def guarded(monkeypatch):
    """The guard on, as in production: conftest turns it off for MockTransport tests."""
    monkeypatch.delenv(_ALLOW)


async def test_a_local_llm_endpoint_is_read_while_records_there_stay_refused(
    model, guarded, monkeypatch
):
    origin, hits = model
    monkeypatch.setenv("LLM_API_BASE", f"{origin}/v1")
    async with server._http_client() as client:
        assert await llm.complete_json(client, system="s", user="u") == _LLAMA_OBJECT
        # Negative, same client, same origin: a record URL pointing there is still refused.
        with pytest.raises(ValidationError, match=_REFUSED):
            await client.get(f"{origin}/api/tags")
    assert hits == ["POST /v1/chat/completions"]


async def test_a_local_embedding_endpoint_is_read_while_records_there_stay_refused(
    model, guarded, monkeypatch
):
    origin, hits = model
    monkeypatch.setenv("EMBEDDING_API_BASE", f"{origin}/v1/")
    async with server._http_client() as client:
        assert await embeddings.embed(client, ["q", "a"]) == [[1.0, 0.0], [1.0, 1.0]]
        with pytest.raises(ValidationError, match=_REFUSED):
            await client.get(f"{origin}/api/tags")
    assert hits == ["POST /v1/embeddings"]


async def test_a_redirect_out_of_the_configured_origin_is_still_refused(
    model, redirector, guarded, monkeypatch, caplog
):
    """Trust is in the origin the operator wrote down, not in wherever it sends us."""
    origin, hits = model
    # Positive control: the redirect target itself, configured directly, is read.
    monkeypatch.setenv("LLM_API_BASE", f"{origin}/v1")
    async with server._http_client() as client:
        assert await llm.complete_json(client, system="s", user="u") == _LLAMA_OBJECT
    assert hits == ["POST /v1/chat/completions"]

    monkeypatch.setenv("LLM_API_BASE", f"{redirector}/v1")
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.llm"):
        async with server._http_client() as client:
            assert await llm.complete_json(client, system="s", user="u") is None
    [message] = caplog.messages
    assert message.startswith(
        "query understanding skipped: [ValidationError] request: '127.0.0.1' resolves to "
    )
    assert hits == ["POST /v1/chat/completions"]  # the redirect target was not reached


async def test_a_request_marked_for_another_origin_is_checked(model, guarded):
    """The mark names its origin, so it cannot be carried to a different one."""
    origin, hits = model
    other = httpx.URL(origin).copy_with(port=httpx.URL(origin).port + 1)
    async with server._http_client() as client:
        with pytest.raises(ValidationError, match=_REFUSED):
            await client.get(origin, extensions=egress.operator_configured(str(other)))
        # Positive control: marked for its own origin, the same request goes through.
        resp = await client.get(f"{origin}/", extensions=egress.operator_configured(origin))
    assert resp.text == "internal"
    assert hits == ["GET /"]


@pytest.mark.parametrize(
    ("configured", "port", "other"),
    [
        ("http://127.0.0.1:80/v1", 80, "http://127.0.0.1:8080/v1"),
        ("https://127.0.0.1:443/v1", 443, "https://127.0.0.1:4443/v1"),
    ],
)
async def test_a_default_port_written_out_matches_the_request_httpx_builds(
    configured, port, other, guarded
):
    """httpx drops a default port from the request URL, so a base written with one
    (``http://host:80``) must still match it. Hook-level: nothing listens on 80/443."""
    url = f"{configured}/chat/completions"
    request = httpx.Request("POST", url, extensions=egress.operator_configured(configured))
    assert f":{port}/" not in str(request.url)  # the premise: httpx dropped the port
    await egress.enforce_on_request(request)  # passed, not refused
    # Negative control: marked for another port on the same host, it is checked.
    marked_other = httpx.Request("POST", url, extensions=egress.operator_configured(other))
    with pytest.raises(ValidationError, match=_REFUSED):
        await egress.enforce_on_request(marked_other)
