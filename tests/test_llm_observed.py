"""The exact request ``llm.complete_json`` sends, and the configuration it is built from."""

from __future__ import annotations

import json

import httpx
import pytest

from data_aggregator_mcp import llm


async def _sent() -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"k": 1}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await llm.complete_json(client, system="sys prompt", user="rna in maize")
    assert out == {"k": 1}
    return sent


async def test_the_request_is_one_json_post_to_chat_completions(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_MODEL", "m")
    [req] = await _sent()
    assert req.method == "POST"
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer sk-x"
    assert json.loads(req.content) == {
        "model": "m",
        "messages": [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "rna in maize"},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


async def test_a_keyless_request_names_the_default_model(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "http://llm.test:11434/v1")
    [req] = await _sent()
    assert "Authorization" not in req.headers
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content)["model"] == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("https://llm.test/v1", "https://llm.test/v1/chat/completions"),
        ("https://llm.test/v1///", "https://llm.test/v1/chat/completions"),
        # Only slashes are stripped: a path ending in X keeps it.
        ("https://llm.test/proxyX/", "https://llm.test/proxyX/chat/completions"),
    ],
)
async def test_trailing_slashes_and_only_those_are_stripped_from_the_base(base, url, monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", base)
    [req] = await _sent()
    assert str(req.url) == url
