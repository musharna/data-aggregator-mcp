"""Streamable HTTP transport — serve the same six tools over HTTP instead of stdio.

The MCP `Server` object and every handler are shared verbatim with the stdio path
(`server.py`); this module only supplies a different pair of streams. Adding it
costs no new dependencies — starlette and uvicorn already ship transitively with
the `mcp` SDK.

SECURITY POSTURE
================
DNS-rebinding protection is **always on** here. The SDK's
``TransportSecurityMiddleware`` disables it when ``security_settings`` is None
(backwards compatibility), so passing None would silently serve a browser-reachable
endpoint that any web page could drive. We never pass None.

Two rules follow from that:

* **Loopback bind (the default)** auto-derives ``allowed_hosts`` / ``allowed_origins``
  from the bound host and port. Nothing to configure.
* **Non-loopback bind** (``--host 0.0.0.0``, a LAN address, a container interface)
  REQUIRES at least one explicit ``--allow-host``. We refuse to start otherwise
  rather than guessing a host allowlist — a wrong guess here is exactly the
  GHSA-vj7q-gjh5-988w / GHSA-jpw9-pfvf-9f58 failure mode. Fail loud, not open.

FETCH SEMANTICS DIFFER OVER HTTP
================================
`fetch(dest=...)` writes to the filesystem of the **server** process. Over stdio
the server is a local child of the client, so that is the user's own disk. Over
HTTP the server may be on another machine, and the caller receives paths it cannot
read. This is inherent to running a filesystem-writing tool remotely; callers that
need bytes locally should use stdio, or treat the HTTP deployment's `dest` as a
server-side staging area. `search` / `resolve` / `operate` / `relate` /
`list_sources` are unaffected — they return data, not paths.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
from collections.abc import AsyncGenerator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.server import server

logger = logging.getLogger(__name__)

#: Path the streamable-HTTP endpoint is mounted at.
MCP_PATH = "/mcp"

#: Sessions with no HTTP traffic for this long are reaped. The SDK recommends
#: 1800s; our tool calls are network-bound but none run for half an hour.
SESSION_IDLE_TIMEOUT = 1800.0

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _is_loopback(host: str) -> bool:
    """True when ``host`` names only the local machine.

    A hostname we cannot parse as an IP is treated as NON-loopback: the safe
    direction is to demand an explicit allowlist, not to assume locality.
    """
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def build_security_settings(
    host: str,
    port: int,
    allow_hosts: list[str] | None = None,
    allow_origins: list[str] | None = None,
) -> TransportSecuritySettings:
    """Assemble DNS-rebinding settings, or fail loud if they cannot be derived.

    Raises ``ValidationError`` when binding a non-loopback interface without an
    explicit ``allow_hosts`` — see the module docstring.
    """
    allow_hosts = list(allow_hosts or [])
    allow_origins = list(allow_origins or [])

    if not allow_hosts:
        if not _is_loopback(host):
            raise ValidationError(
                f"refusing to serve on non-loopback host {host!r} without an explicit "
                f"host allowlist: pass --allow-host <host:port> (repeatable) naming every "
                f"Host header clients will send. Binding a wildcard/LAN interface with an "
                f"auto-guessed allowlist is the DNS-rebinding hole this transport exists to "
                f"avoid. To serve only this machine, drop --host (defaults to {DEFAULT_HOST})."
            )
        allow_hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]

    if not allow_origins:
        # Only meaningful for browser clients; a request with no Origin header
        # (every CLI/SDK client) is allowed by the SDK regardless.
        allow_origins = [f"http://{h}" for h in allow_hosts] + [f"https://{h}" for h in allow_hosts]

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allow_hosts,
        allowed_origins=allow_origins,
    )


def build_app(
    security_settings: TransportSecuritySettings,
    *,
    stateless: bool = False,
    json_response: bool = False,
) -> Starlette:
    """Build the Starlette app exposing the MCP server at :data:`MCP_PATH`."""
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=stateless,
        json_response=json_response,
        security_settings=security_settings,
        # session_idle_timeout is rejected in stateless mode by the SDK.
        session_idle_timeout=None if stateless else SESSION_IDLE_TIMEOUT,
    )

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        # The manager's task group lives for the process; it cannot be restarted.
        async with manager.run():
            yield

    return Starlette(routes=[Mount(MCP_PATH, app=handle)], lifespan=lifespan)


def serve_http(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_hosts: list[str] | None = None,
    allow_origins: list[str] | None = None,
    stateless: bool = False,
    json_response: bool = False,
    log_level: str = "info",
) -> None:
    """Run the MCP server over streamable HTTP (blocking)."""
    settings = build_security_settings(host, port, allow_hosts, allow_origins)
    app = build_app(settings, stateless=stateless, json_response=json_response)
    logger.info(
        "data-aggregator-mcp streamable HTTP on http://%s:%s%s "
        "(dns_rebinding_protection=on, allowed_hosts=%s, stateless=%s)",
        host,
        port,
        MCP_PATH,
        settings.allowed_hosts,
        stateless,
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)
