import httpx
import pytest

from data_aggregator_mcp import _http, _ratelimit


@pytest.mark.asyncio
async def test_request_acquires_a_token_per_send(httpx_mock, monkeypatch):
    calls: list[str] = []

    async def fake_acquire(service: str) -> None:
        calls.append(service)

    monkeypatch.setattr(_ratelimit, "acquire", fake_acquire)
    httpx_mock.add_response(url="https://example.test/x", json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _http.request_json(client, "GET", "https://example.test/x", service="Zenodo search")

    assert calls == ["Zenodo search"]
