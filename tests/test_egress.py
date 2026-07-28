"""The egress guard: a record's URL must not point into private address space.

Every test here deletes DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS first, because conftest
sets it for the rest of the suite (mocked transports never egress, and resolving the
fixtures' fictional hosts would put real DNS on the critical path). So this file is the
only place the guard is actually exercised — which is why it also covers the integration
points rather than just the helper.

Addresses are IP LITERALS throughout: getaddrinfo returns them without a DNS query, so
these are hermetic and fast, and a test cannot pass merely because a name failed to resolve.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import egress, operate
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import DataResource, FileEntry

pytestmark = pytest.mark.asyncio


@pytest.fixture
def guard_on(monkeypatch):
    """Turn the guard on (conftest turns it off) and start from a cold cache."""
    monkeypatch.delenv(egress.ALLOW_PRIVATE_ENV, raising=False)
    egress._clear_cache()


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://127.0.0.1:9200/a.csv", "IPv4 loopback"),
        ("http://[::1]:9200/a.csv", "IPv6 loopback"),
        ("http://10.0.0.5/a.csv", "RFC1918"),
        ("http://192.168.1.10/a.csv", "RFC1918"),
        ("http://172.16.0.1/a.csv", "RFC1918"),
        ("http://169.254.169.254/latest/meta-data/", "link-local: cloud metadata"),
        ("http://[fd00::1]/a.csv", "IPv6 unique-local"),
        ("http://0.0.0.0/a.csv", "unspecified"),
    ],
)
async def test_private_addresses_are_refused(guard_on, url: str, why: str) -> None:
    with pytest.raises(ValidationError) as exc:
        await egress.assert_public_url(url, what="probe")
    # The message has to name the address, or an operator cannot tell why it fired.
    assert "non-public" in str(exc.value), why


@pytest.mark.parametrize("url", ["http://8.8.8.8/a.csv", "https://1.1.1.1/a.csv"])
async def test_public_addresses_are_allowed(guard_on, url: str) -> None:
    """Positive control. Refusing everything would satisfy the test above and break fetch."""
    await egress.assert_public_url(url, what="probe")


async def test_opt_out_env_allows_private(monkeypatch) -> None:
    """Operators who really do serve records from private space have a way out."""
    monkeypatch.setenv(egress.ALLOW_PRIVATE_ENV, "1")
    egress._clear_cache()
    await egress.assert_public_url("http://127.0.0.1:9200/a.csv", what="probe")


async def test_unresolvable_host_is_allowed(guard_on) -> None:
    """Documented and deliberate: a name with no address points nowhere, so there is
    nothing to reach and the request fails later with an accurate transport error.
    Refusing here would block every offline caller while closing nothing."""
    await egress.assert_public_url("https://no-such-host.invalid/a.csv", what="probe")


async def test_non_http_schemes_are_not_our_business(guard_on) -> None:
    """Scheme policy belongs to the callers, which gate before calling this. A file://
    URL has no host and must not be reported as an egress problem."""
    await egress.assert_public_url("file:///tmp/a.csv", what="probe")


async def test_a_refusal_is_never_cached_as_an_approval(guard_on) -> None:
    """The cache exists so a 4-file record does not resolve one host four times. It must
    only ever remember successes — caching a rejection as an approval would turn the
    optimisation into a bypass."""
    for _ in range(3):
        with pytest.raises(ValidationError):
            await egress.assert_public_url("http://127.0.0.1:9200/a.csv", what="probe")
    assert not egress._approved, "a rejected target was recorded as approved"


# --- the integration points: this is the actual vulnerability ------------------------


def _poisoned(url: str) -> DataResource:
    """A record whose file NAME looks tabular while its URL points wherever the uploader
    likes. `operate._operable` checks the name, never the url — which is what made the
    source URL reachable in the first place."""
    return DataResource(
        id="zenodo:999999",
        source="zenodo",
        kind="dataset",
        title="looks legit",
        files=[FileEntry(name="data.csv", url=url, checksum=None)],
    )


async def test_operate_refuses_a_source_url_in_private_space(guard_on, monkeypatch) -> None:
    """duckquery locks the filesystems AFTER materializing the source, of necessity, so
    the lock cannot protect the source read. Without this guard, `operate(op='head')` on a
    poisoned record fetched the address and returned the body to the caller."""

    async def fake_resolve(client, resource_id):
        return _poisoned("http://127.0.0.1:9/anything.csv")

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValidationError) as exc:
            await operate.run(client, "zenodo:999999", op="head", n=5)
    assert "non-public" in str(exc.value)


async def test_fetch_refuses_a_url_in_private_space(guard_on, tmp_path) -> None:
    """Same exposure on the fetch path, where the body lands on disk instead."""
    resource = _poisoned("http://127.0.0.1:9/anything.csv")
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValidationError) as exc:
            await fetch_mod.fetch_files(client, resource, dest=str(tmp_path))
    assert "non-public" in str(exc.value)


async def test_operate_still_accepts_a_public_source(guard_on, monkeypatch) -> None:
    """Positive control for the integration: the guard must not refuse ordinary records.

    Proves it by tripping a sentinel in the step that runs immediately AFTER the guard —
    reaching that step is only possible if the guard let the URL past. The first version
    of this test let `operate.run` proceed to a real connection to 8.8.8.8:80 and hung for
    61 seconds against operate's own wall-clock timeout; a unit test has no business
    leaving the process to prove a local decision.
    """

    class _ReachedTheNextStep(RuntimeError):
        pass

    async def fake_resolve(client, resource_id):
        return _poisoned("http://8.8.8.8/anything.csv")

    def boom(url: str):
        raise _ReachedTheNextStep(url)

    monkeypatch.setattr("data_aggregator_mcp.router.resolve", fake_resolve)
    monkeypatch.setattr("data_aggregator_mcp.operate._source_size", boom)

    async with httpx.AsyncClient() as client:
        with pytest.raises(_ReachedTheNextStep):
            await operate.run(client, "zenodo:999999", op="head", n=5)


async def test_a_redirect_into_private_space_is_blocked(guard_on, tmp_path, monkeypatch) -> None:
    """The bypass that shipped in v0.45.2: checking the URL we are HANDED is not enough.

    The client follows redirects, so a record with a perfectly public URL that 302s to
    `http://127.0.0.1/` reached it with the call-site check satisfied — measured at the
    time as `guard consulted about: [entry]`, `redirect target hits: 1`. The fix is a
    request event hook, because that is the only layer that sees every address actually
    connected to.

    The entry URL is waved through here to stand in for a public origin; every other hop
    goes through the real guard, so a pass cannot come from the harness disabling it.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

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

    target_srv = HTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=target_srv.serve_forever, daemon=True).start()
    target_url = f"http://127.0.0.1:{target_srv.server_address[1]}/secret.csv"

    class _Redirector(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

    redir_srv = HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=redir_srv.serve_forever, daemon=True).start()
    entry = f"http://127.0.0.1:{redir_srv.server_address[1]}/looks-public.csv"

    real = egress.assert_public_url

    async def entry_is_public(url: str, *, what: str) -> None:
        if url == entry:
            return
        await real(url, what=what)

    monkeypatch.setattr(egress, "assert_public_url", entry_is_public)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            event_hooks={"request": [egress.enforce_on_request]},
        ) as client:
            with pytest.raises(ValidationError):
                await fetch_mod.fetch_files(client, _poisoned(entry), dest=str(tmp_path))
    finally:
        redir_srv.shutdown()
        target_srv.shutdown()

    assert hits == [], "the redirect target was contacted despite the guard"
