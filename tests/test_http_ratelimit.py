import httpx
import pytest

from data_aggregator_mcp import _http, _ratelimit


@pytest.mark.asyncio
async def test_request_acquires_a_token_per_send(httpx_mock, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_acquire(service: str, url: str) -> None:
        calls.append((service, url))

    monkeypatch.setattr(_ratelimit, "acquire", fake_acquire)
    httpx_mock.add_response(url="https://example.test/x", json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _http.request_json(client, "GET", "https://example.test/x", service="Zenodo search")

    # The URL has to reach the limiter, not just the label — the bucket is keyed on host.
    assert calls == [("Zenodo search", "https://example.test/x")]
