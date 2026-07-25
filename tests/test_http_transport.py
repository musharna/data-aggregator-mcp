"""Streamable HTTP transport — security-settings unit tests + a real-execution check.

The unit tests below pin the fail-loud allowlist policy. They are NOT sufficient on
their own: a synthetic Starlette app can pass every one of them while the packaged
entry point fails to serve. ``test_entrypoint_serves_over_streamable_http`` closes
that gap by launching the real console script on a real port and driving a real MCP
initialize + list_tools handshake, mirroring the stdio check in
``test_entrypoint_smoke.py``.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from collections.abc import Generator

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from data_aggregator_mcp import __version__, http_transport
from data_aggregator_mcp.errors import ValidationError

EXPECTED_TOOLS = {"search", "resolve", "fetch", "list_sources", "operate", "relate"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------
# Security policy
# --------------------------------------------------------------------------


def test_loopback_derives_allowlist_without_configuration() -> None:
    settings = http_transport.build_security_settings("127.0.0.1", 8000)
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["127.0.0.1:8000", "localhost:8000", "[::1]:8000"]
    # Origins are derived for browser clients over both schemes.
    assert "http://127.0.0.1:8000" in settings.allowed_origins
    assert "https://localhost:8000" in settings.allowed_origins


@pytest.mark.parametrize("host", ["localhost", "::1", "127.0.0.53"])
def test_loopback_forms_all_auto_derive(host: str) -> None:
    assert http_transport.build_security_settings(host, 1234).allowed_hosts


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "mcp.example.com"])
def test_non_loopback_without_allowlist_fails_loud(host: str) -> None:
    # The whole point of the transport: refuse to guess a Host allowlist off
    # loopback rather than silently serving a rebinding-vulnerable endpoint.
    with pytest.raises(ValidationError) as exc:
        http_transport.build_security_settings(host, 8000)
    assert "--allow-host" in str(exc.value)


def test_non_loopback_with_explicit_allowlist_is_accepted() -> None:
    settings = http_transport.build_security_settings(
        "0.0.0.0", 8000, allow_hosts=["mcp.example.com:8000"]
    )
    assert settings.allowed_hosts == ["mcp.example.com:8000"]
    assert settings.enable_dns_rebinding_protection is True


def test_unparseable_hostname_is_treated_as_non_loopback() -> None:
    # Fail-safe direction: anything we cannot prove is local demands an allowlist.
    assert http_transport._is_loopback("not-an-ip") is False


def test_explicit_origins_are_not_overridden() -> None:
    settings = http_transport.build_security_settings(
        "127.0.0.1", 8000, allow_origins=["https://app.example.com"]
    )
    assert settings.allowed_origins == ["https://app.example.com"]


def test_dns_rebinding_protection_is_never_disabled() -> None:
    # Regression guard for GHSA-vj7q-gjh5-988w / GHSA-jpw9-pfvf-9f58: the SDK
    # middleware silently disables protection when handed None, so every path
    # out of build_security_settings must return it enabled.
    for kwargs in ({}, {"allow_hosts": ["h:1"]}, {"allow_origins": ["https://o"]}):
        settings = http_transport.build_security_settings("127.0.0.1", 1, **kwargs)  # type: ignore[arg-type]
        assert settings.enable_dns_rebinding_protection is True


def test_stateless_app_builds_without_idle_timeout() -> None:
    # The SDK raises if session_idle_timeout is combined with stateless.
    settings = http_transport.build_security_settings("127.0.0.1", 8000)
    assert http_transport.build_app(settings, stateless=True) is not None
    assert http_transport.build_app(settings, stateless=False) is not None


async def test_lifespan_installs_the_shared_http_client() -> None:
    """E4: HTTP requests go through the same _dispatch as stdio calls, so the app's
    lifespan must scope the shared client the same way `_serve` does — otherwise the
    HTTP deployment silently keeps paying a new TLS handshake per request."""
    from data_aggregator_mcp import server as server_mod

    settings = http_transport.build_security_settings("127.0.0.1", 8000)
    app = http_transport.build_app(settings)
    assert server_mod._SHARED_CLIENT is None
    async with app.router.lifespan_context(app):
        client = server_mod._SHARED_CLIENT
        assert client is not None and not client.is_closed
    assert server_mod._SHARED_CLIENT is None
    assert client.is_closed  # shut down with the app, not leaked


# --------------------------------------------------------------------------
# Real execution — packaged entry point, real port, real handshake
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _live_server(*extra: str) -> Generator[int]:
    """Launch the packaged entry point over HTTP on a free port; yield the port."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "data_aggregator_mcp", "--transport", "http", "--port", str(port)]
        + list(extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                _, err = proc.communicate()
                pytest.fail(f"server exited early: {err.decode(errors='replace')}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() > deadline:  # pragma: no cover
                    pytest.fail("server did not accept connections within 30s")
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=10)


async def test_entrypoint_serves_over_streamable_http() -> None:
    with _live_server() as port:
        url = f"http://127.0.0.1:{port}{http_transport.MCP_PATH}"
        async with (
            streamable_http_client(url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            tools = await session.list_tools()
    assert init.serverInfo.name == "data-aggregator-mcp"
    # Regression guard: the SDK reports its OWN version when Server(version=)
    # is omitted, which told clients the server was "1.28.1".
    assert init.serverInfo.version == __version__
    assert {t.name for t in tools.tools} == EXPECTED_TOOLS


def test_live_server_rejects_spoofed_host_and_origin() -> None:
    """DNS-rebinding protection must be live on the wire, not just in settings.

    Asserting on ``TransportSecuritySettings`` alone would pass even if the
    middleware were never wired up, so this drives real HTTP requests. Note the
    trailing slash: Starlette's Mount 307-redirects the bare path, and a request
    that only gets redirected never reaches the security middleware at all.
    """
    with _live_server() as port:
        url = f"http://127.0.0.1:{port}{http_transport.MCP_PATH}/"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        with httpx.Client(timeout=15) as client:
            ok = client.post(url, json=body, headers=headers)
            bad_host = client.post(url, json=body, headers={**headers, "Host": "evil.example"})
            bad_origin = client.post(
                url, json=body, headers={**headers, "Origin": "https://evil.example"}
            )

    assert ok.status_code == 200, ok.text
    assert bad_host.status_code == 421, bad_host.text
    assert bad_origin.status_code == 403, bad_origin.text


def test_entrypoint_refuses_non_loopback_bind_without_allow_host() -> None:
    # Real-execution counterpart to the unit test: the CLI must exit non-zero
    # with an actionable message, not start a wide-open listener.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_aggregator_mcp",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            str(_free_port()),
        ],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "--allow-host" in proc.stderr.decode(errors="replace")


def test_bare_invocation_still_defaults_to_stdio() -> None:
    # Guard the compatibility contract: no args = stdio, exactly as before.
    from data_aggregator_mcp import server as server_mod

    called: dict[str, bool] = {}

    async def fake_serve() -> None:
        called["stdio"] = True

    original = server_mod._serve
    server_mod._serve = fake_serve  # type: ignore[assignment]
    try:
        server_mod.main([])
    finally:
        server_mod._serve = original  # type: ignore[assignment]
    assert called == {"stdio": True}
