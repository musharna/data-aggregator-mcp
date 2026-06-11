from __future__ import annotations

import json

import httpx
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import llm


def test_config_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    assert llm._config() is None


def test_config_defaults_model_and_strips_base(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1/")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    base, key, model = llm._config()
    assert base == "https://llm.test/v1"
    assert key is None
    assert model == "gpt-4o-mini"


async def test_complete_json_returns_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    async with httpx.AsyncClient() as client:
        assert await llm.complete_json(client, system="s", user="u") is None


def _chat_response(content: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


async def test_complete_json_posts_and_parses(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_MODEL", "m")
    httpx_mock.add_response(
        url="https://llm.test/v1/chat/completions",
        json=_chat_response({"keyword_core": "rna", "organism": "Zea mays"}),
    )
    async with httpx.AsyncClient() as client:
        out = await llm.complete_json(client, system="sys", user="rna in maize")
    assert out == {"keyword_core": "rna", "organism": "Zea mays"}
    req = httpx_mock.get_requests()[0]
    assert req.headers["authorization"] == "Bearer sk-x"
    body = json.loads(req.content)
    assert body["model"] == "m"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "rna in maize"},
    ]


async def test_complete_json_no_auth_header_when_keyless(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://llm.test/v1/chat/completions",
        json=_chat_response({"keyword_core": "x"}),
    )
    async with httpx.AsyncClient() as client:
        out = await llm.complete_json(client, system="s", user="u")
    assert out == {"keyword_core": "x"}
    req = httpx_mock.get_requests()[0]
    assert "authorization" not in req.headers


async def test_complete_json_none_on_unparseable_content(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    httpx_mock.add_response(
        url="https://llm.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "not json {{"}}]},
    )
    async with httpx.AsyncClient() as client:
        assert await llm.complete_json(client, system="s", user="u") is None


async def test_complete_json_none_when_content_is_not_object(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    httpx_mock.add_response(
        url="https://llm.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "[1, 2, 3]"}}]},
    )
    async with httpx.AsyncClient() as client:
        assert await llm.complete_json(client, system="s", user="u") is None


async def test_complete_json_none_on_missing_choices(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    httpx_mock.add_response(url="https://llm.test/v1/chat/completions", json={"unexpected": True})
    async with httpx.AsyncClient() as client:
        assert await llm.complete_json(client, system="s", user="u") is None


async def test_complete_json_none_on_transport_error(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    # _http retries transport errors up to max_retries; register reusably.
    httpx_mock.add_exception(httpx.ConnectError("boom"), is_reusable=True)
    async with httpx.AsyncClient() as client:
        # must NEVER raise into the search path
        assert await llm.complete_json(client, system="s", user="u") is None
