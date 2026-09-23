from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError


async def test_returns_response_on_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://x.test/ok", json={"a": 1})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(client, "GET", "https://x.test/ok", service="t")
    assert resp.json() == {"a": 1}


async def test_404_raises_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://x.test/missing", status_code=404, text="nope")
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await _http.request_with_retry(client, "GET", "https://x.test/missing", service="t")


async def test_retries_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://x.test/r", status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url="https://x.test/r", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(client, "GET", "https://x.test/r", service="t")
    assert resp.json() == {"ok": True}


async def test_404_sentinel_suppresses_raise(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://x.test/s", status_code=404)
    async with httpx.AsyncClient() as client:
        out = await _http.request_with_retry(
            client, "GET", "https://x.test/s", service="t", not_found_returns=None
        )
    assert out is None


async def test_retries_transport_error_then_succeeds(httpx_mock: HTTPXMock, monkeypatch) -> None:
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    httpx_mock.add_exception(httpx.ConnectError("boom"), url="https://x.test/t")
    httpx_mock.add_response(url="https://x.test/t", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(client, "GET", "https://x.test/t", service="t")
    assert resp.json() == {"ok": True}


async def test_transport_error_exhausted_raises_upstream(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectTimeout("slow"), url="https://x.test/d")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_with_retry(client, "GET", "https://x.test/d", service="t")


async def test_real_connection_refused_becomes_upstream_error() -> None:
    # Real transport failure (connection refused on a closed local port) must
    # surface as the typed taxonomy error, not a raw httpx.ConnectError.
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_with_retry(
                client, "GET", "http://127.0.0.1:1/x", service="probe", max_retries=1
            )


async def test_request_json_retries_malformed_then_succeeds(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    httpx_mock.add_response(url="https://x.test/j", text="{bad json")  # 200, unparseable
    httpx_mock.add_response(url="https://x.test/j", json={"ok": 1})
    async with httpx.AsyncClient() as client:
        data = await _http.request_json(client, "GET", "https://x.test/j", service="t")
    assert data == {"ok": 1}


async def test_request_json_terminal_malformed_raises_upstream(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(url="https://x.test/jj", text="<html>throttled</html>")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_json(client, "GET", "https://x.test/jj", service="t")


async def test_request_xml_retries_malformed_then_succeeds(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    httpx_mock.add_response(url="https://x.test/x", text="<broken")  # 200, unparseable XML
    httpx_mock.add_response(url="https://x.test/x", text="<ok/>")
    async with httpx.AsyncClient() as client:
        resp = await _http.request_xml(client, "GET", "https://x.test/x", service="t")
    assert resp.text == "<ok/>"


# --- audit 2026-09-22: M14 (2xx other than 200) and H3 (wrong-shape 200 body) ---


async def test_204_no_content_is_success_not_outage(httpx_mock: HTTPXMock) -> None:
    """RCSB answers a zero-hit search with 204 No Content. Only 200 counted as success,
    so a legitimate "no hits" exhausted the retries and surfaced as an outage."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(url="https://x.test/empty", status_code=204)
    httpx_mock.add_response(url="https://x.test/full", json={"hits": [1]})
    httpx_mock.add_response(url="https://x.test/down", status_code=500, is_reusable=True)
    async with httpx.AsyncClient() as client:
        out = await _http.request_json(
            client, "GET", "https://x.test/empty", service="t", no_content_returns={"hits": []}
        )
        assert out == {"hits": []}
        # positive control: a 200 body still parses through the same call shape
        full = await _http.request_json(
            client, "GET", "https://x.test/full", service="t", no_content_returns={"hits": []}
        )
        assert full == {"hits": [1]}
        # and a real outage is still an outage (no_content_returns does not mask 5xx)
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_json(
                client,
                "GET",
                "https://x.test/down",
                service="t",
                max_retries=1,
                no_content_returns={"hits": []},
            )


async def test_204_without_sentinel_raises_upstream(httpx_mock: HTTPXMock) -> None:
    """A caller that did not opt in cannot parse an empty body: fail loud, typed."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(url="https://x.test/e", status_code=204, is_reusable=True)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="no content"):
            await _http.request_json(client, "GET", "https://x.test/e", service="t", max_retries=1)


async def test_request_json_expect_rejects_wrong_shape(httpx_mock: HTTPXMock) -> None:
    """A 200 error envelope (dict) where a list is expected is an upstream failure,
    not an empty result set."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(
        url="https://x.test/list", json={"detail": "Internal error"}, is_reusable=True
    )
    httpx_mock.add_response(url="https://x.test/ok", json=[{"a": 1}])
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="expected list"):
            await _http.request_json(
                client, "GET", "https://x.test/list", service="t", max_retries=1, expect=list
            )
        ok = await _http.request_json(client, "GET", "https://x.test/ok", service="t", expect=list)
    assert ok == [{"a": 1}]
