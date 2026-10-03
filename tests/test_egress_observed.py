"""The egress guard's observable contract, pinned exactly (#88 mutant burn-down).

`test_egress.py` covers the categories and the integration points; this file pins what a
caller and an operator see: the exact refusal text, which addresses `is_global` alone
would let through, the one lookup per host the approval cache promises and the edge of
its window, the hook's own label, and that a refused URL is never connected to.

Every test starts with the guard on (conftest turns it off for the rest of the suite).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from data_aggregator_mcp import egress
from data_aggregator_mcp.errors import ValidationError

pytestmark = pytest.mark.asyncio

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@pytest.fixture
def guard_on(monkeypatch):
    """Turn the guard on (conftest turns it off) and start from a cold cache."""
    monkeypatch.delenv(egress.ALLOW_PRIVATE_ENV, raising=False)
    egress._clear_cache()


def _refusal(what: str, host: str, ip: str, url: str) -> str:
    """The full text of an address refusal, written out rather than imported."""
    return (
        f"[ValidationError] {what}: {host!r} resolves to the non-public address {ip}; "
        f"refusing to fetch {url}. A record pointing into private address space would "
        f"make this server read something the caller cannot reach itself. Set "
        f"DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS=1 only if you deliberately serve "
        f"records from there."
    )


def _spy_resolver(monkeypatch, answer=None) -> list[tuple]:
    """Record every lookup the guard makes on the running loop. With *answer*, return it
    instead of resolving (a fixed list of addresses); otherwise resolve for real."""
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo
    calls: list[tuple] = []

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        if answer is None:
            return await real(*args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in answer]

    monkeypatch.setattr(loop, "getaddrinfo", spy)
    return calls


def _lookup(host: str) -> tuple:
    return ((host, None), {"proto": socket.IPPROTO_TCP})


# --- what is_global alone would let through ----------------------------------------


@pytest.mark.parametrize(
    ("url", "host", "ip", "why"),
    [
        ("http://224.0.0.1/a.csv", "224.0.0.1", "224.0.0.1", "IPv4 multicast"),
        ("http://[ff0e::1]/a.csv", "ff0e::1", "ff0e::1", "global-scope IPv6 multicast"),
        ("http://[::7f00:1]/a.csv", "::7f00:1", "::7f00:1", "IPv4-compatible ::127.0.0.1"),
        (
            "http://[64:ff9b::7f00:1]/a.csv",
            "64:ff9b::7f00:1",
            "64:ff9b::7f00:1",
            "NAT64 of loopback",
        ),
    ],
)
async def test_addresses_the_stdlib_calls_global_are_refused(guard_on, url, host, ip, why) -> None:
    """`is_global` is True for each of these, so the guard's two named categories
    (multicast, reserved) are the only thing refusing them."""
    assert ipaddress.ip_address(ip).is_global, f"premise: the stdlib calls {ip} global"
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) == _refusal("probe", host, ip, url), why
    # positive control, same family through the same call: a public address passes
    await egress.assert_public_url("http://8.8.8.8/a.csv", what="probe")
    await egress.assert_public_url("http://[2001:4860:4860::8888]/a.csv", what="probe")


@pytest.mark.parametrize(
    ("url", "host", "ip"),
    [
        ("https://127.0.0.1/a.csv", "127.0.0.1", "127.0.0.1"),
        ("https://[::1]:8443/a.csv", "::1", "::1"),
        ("HTTPS://10.0.0.5/a.csv", "10.0.0.5", "10.0.0.5"),
    ],
)
async def test_https_into_private_space_is_refused_like_http(guard_on, url, host, ip) -> None:
    """The scheme gate passes http AND https to the address check; every other refusal
    test used http, so dropping https from it went unseen."""
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) == _refusal("probe", host, ip, url)
    await egress.assert_public_url("https://8.8.8.8/a.csv", what="probe")


async def test_a_url_without_a_host_is_refused_with_its_own_message(guard_on) -> None:
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url("http:///a.csv", what="probe")
    assert (
        str(exc.value)
        == "[ValidationError] probe: URL has no host; refusing to fetch http:///a.csv"
    )
    await egress.assert_public_url("http://8.8.8.8/a.csv", what="probe")


# --- the resolver answer -------------------------------------------------------------


async def test_one_private_address_among_public_ones_refuses_the_name(
    guard_on, monkeypatch
) -> None:
    """A name answering with one public and one loopback address is the shape an attacker
    picks; the guard refuses on ANY non-public address, and remembers nothing."""
    _spy_resolver(monkeypatch, answer=["8.8.8.8", "127.0.0.1"])
    url = "http://mixed.example/a.csv"
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) == _refusal("probe", "mixed.example", "127.0.0.1", url)
    assert egress._approved == {}
    _spy_resolver(monkeypatch, answer=["8.8.8.8", "1.1.1.1"])
    await egress.assert_public_url(url, what="probe")
    assert list(egress._approved) == ["mixed.example"]


async def test_an_address_the_resolver_cannot_parse_is_refused_naming_it(
    guard_on, monkeypatch
) -> None:
    _spy_resolver(monkeypatch, answer=["not-an-ip"])
    url = "http://odd.example/a.csv"
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) == (
        "[ValidationError] probe: 'odd.example' resolved to an unparseable address "
        f"'not-an-ip'; refusing to fetch {url}"
    )
    _spy_resolver(monkeypatch, answer=["8.8.8.8"])
    await egress.assert_public_url(url, what="probe")


async def test_a_name_resolving_to_loopback_is_refused_by_the_real_resolver(guard_on) -> None:
    """No stub: `localhost` comes from the hosts file, so this is the real lookup path
    without a network query."""
    url = "http://localhost:9/a.csv"
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) in {
        _refusal("probe", "localhost", "127.0.0.1", url),
        _refusal("probe", "localhost", "::1", url),
    }
    await egress.assert_public_url("http://8.8.8.8:9/a.csv", what="probe")


# --- the approval cache ----------------------------------------------------------------


async def test_a_host_is_looked_up_once_whatever_the_port_or_scheme(guard_on, monkeypatch) -> None:
    """The cache exists so a multi-file record does not resolve its host once per file.
    The verdict depends on the addresses only, so the lookup takes no port and one
    approval covers every port and scheme on that host."""
    calls = _spy_resolver(monkeypatch)
    for url in ("http://8.8.8.8/a.csv", "https://8.8.8.8:8443/b.csv", "http://8.8.8.8/c.csv"):
        await egress.assert_public_url(url, what="probe")
    assert calls == [_lookup("8.8.8.8")]
    await egress.assert_public_url("http://1.1.1.1/a.csv", what="probe")
    assert calls == [_lookup("8.8.8.8"), _lookup("1.1.1.1")]
    assert set(egress._approved) == {"8.8.8.8", "1.1.1.1"}


async def test_an_approval_lasts_strictly_less_than_the_window(guard_on, monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    clock = [1000.0]
    monkeypatch.setattr(loop, "time", lambda: clock[0])
    calls = _spy_resolver(monkeypatch)
    url = "http://8.8.8.8/a.csv"

    await egress.assert_public_url(url, what="probe")
    assert egress._approved == {"8.8.8.8": 1000.0}
    clock[0] = 1000.0 + egress._APPROVAL_TTL_S - 0.001
    await egress.assert_public_url(url, what="probe")
    assert len(calls) == 1, "re-resolved inside the window"
    clock[0] = 1000.0 + egress._APPROVAL_TTL_S
    await egress.assert_public_url(url, what="probe")
    assert len(calls) == 2, "an approval exactly one window old was still trusted"
    assert egress._approved == {"8.8.8.8": 1030.0}


# --- the request hook ----------------------------------------------------------------


async def test_the_hook_labels_its_refusal_as_a_request(guard_on) -> None:
    """The hook sees no file name, so its refusals say `request:`; the call-site checks
    are the ones that name the file."""
    url = "http://127.0.0.1:9/a.csv"
    with pytest.raises(ValidationError) as exc:
        await egress.enforce_on_request(httpx.Request("GET", url))
    assert str(exc.value) == _refusal("request", "127.0.0.1", "127.0.0.1", url)
    assert await egress.enforce_on_request(httpx.Request("GET", "http://8.8.8.8/a.csv")) is None


async def test_a_refused_url_is_never_connected_to(guard_on, monkeypatch) -> None:
    """Real execution: a listener on 127.0.0.1 counts connections. Through a client with
    the hook, a request to it raises the refusal and the listener sees nothing. With the
    operator opt-out set, the same request reaches it, so the harness can see a
    connection when one happens."""
    hits: list[str] = []

    class _Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"a,b\n")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        async with httpx.AsyncClient(
            event_hooks={"request": [egress.enforce_on_request]}
        ) as client:
            for url in (
                f"http://127.0.0.1:{port}/secret.csv",
                f"http://localhost:{port}/secret.csv",
            ):
                with pytest.raises(ValidationError) as exc:
                    await client.get(url)
                assert "resolves to the non-public address" in str(exc.value), url
            assert hits == [], "a refused URL was connected to"

            monkeypatch.setenv(egress.ALLOW_PRIVATE_ENV, "1")
            resp = await client.get(f"http://127.0.0.1:{port}/secret.csv")
            assert resp.status_code == 200 and resp.content == b"a,b\n"
            assert hits == ["/secret.csv"]
    finally:
        srv.shutdown()


# --- live: real public DNS -----------------------------------------------------------


@live_only
async def test_live_public_dns_answers_are_judged_both_ways(guard_on, monkeypatch) -> None:
    """Through public DNS: zenodo.org resolves to public addresses and is allowed with
    one lookup; localtest.me is a public name whose A record is 127.0.0.1, which the
    guard must refuse though no hosts file mentions it (it also answers AAAA ::1, and
    whichever comes first is the one named)."""
    calls = _spy_resolver(monkeypatch)
    await egress.assert_public_url("https://zenodo.org/api/records", what="probe")
    await egress.assert_public_url("https://zenodo.org/records/1", what="probe")
    assert calls == [_lookup("zenodo.org")]
    url = "http://localtest.me/a.csv"
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    assert str(exc.value) in {
        _refusal("probe", "localtest.me", "127.0.0.1", url),
        _refusal("probe", "localtest.me", "::1", url),
    }
