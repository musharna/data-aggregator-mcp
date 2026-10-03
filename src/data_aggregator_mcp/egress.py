# src/data_aggregator_mcp/egress.py
"""Refuse record-supplied URLs that point into non-public address space.

A file URL comes from an upstream record, and Zenodo, HuggingFace, figshare and OpenML all
accept user uploads — so it is attacker-controlled input, not trusted configuration. Left
unchecked, publishing a record whose file is *named* ``data.csv`` while its URL points at
``http://127.0.0.1:9200/`` or ``http://169.254.169.254/`` makes the server fetch that
address and hand the body back to the caller.

That is harmless over stdio, where the server is the caller's own child process and shares
its network position. It is SSRF with response exfiltration under ``--transport http``,
where the server may sit in a network the caller cannot otherwise reach.

Why here and not in ``duckquery``: that module locks the filesystems *after* materializing
the source, deliberately and of necessity — a ``CREATE VIEW`` would evaluate after the lock
and block the legitimate read too. So the lock protects the user's SELECT and structurally
cannot protect the source read. The source URL has to be judged before the fetch begins.

And judged at every hop, not only the first: a client that follows redirects by itself
(fsspec's aiohttp session, DuckDB's httpfs) reaches a redirect target this module never
sees. So a record URL is only ever read through an httpx client carrying
``enforce_on_request`` or its sync twin — ``fetch`` through the server's client,
``operate`` through ``sourceio``'s.

KNOWN LIMITATION — this does not defeat DNS rebinding. The name is resolved here and
resolved again by the HTTP client, so a server that answers public-then-private between
those two lookups still wins. Closing that requires pinning the checked address into the
connection itself. This raises the bar from "trivially exploitable by anyone who can upload
a record" to "needs a rebinding-capable resolver", which is worth having while being honest
that it is not total.

What it does not judge: the model endpoints the operator configures (``LLM_API_BASE``,
``EMBEDDING_API_BASE``). Those are trusted configuration, often a local server on
127.0.0.1, yet they share the server's client with record URLs. ``llm`` and ``embeddings``
mark their requests with ``operator_configured``, and the hook passes a marked request only
while it is still addressed to the origin the mark names. A record URL is never marked, so
a record pointing at that same local server is still refused, and so is a redirect that
leaves the configured origin.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import SplitResult, urlsplit

from data_aggregator_mcp.errors import ValidationError

#: Operators who genuinely serve records from private address space — an on-prem mirror, a
#: dev fixture — set this to "1". Named for what it permits, so it cannot be mistaken for a
#: performance knob.
ALLOW_PRIVATE_ENV = "DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS"

#: httpx request extension carrying the origin of an operator-configured endpoint. httpx
#: copies extensions onto a redirect, which is why the hook compares the origin, not only
#: the presence of the mark.
_OPERATOR_CONFIGURED = "data_aggregator_mcp.operator_configured"
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Seconds an approved host stays approved. Files in one record almost always share
#: a host, so without this a 4-file fetch pays four resolutions of the same name — enough to
#: measurably serialize a parallel download. Deliberately SHORT, and only successful
#: verdicts are cached: a longer window would widen the DNS-rebinding gap the module
#: docstring already declines to defend against, and there is no reason to also make it
#: last minutes.
_APPROVAL_TTL_S = 30.0
_approved: dict[str, float] = {}


def _clear_cache() -> None:
    """Drop cached approvals. For tests that flip the env var between assertions."""
    _approved.clear()


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for addresses that are routable on the public internet.

    Judged by stdlib *categories* rather than a list of ranges, which would need editing
    every time IANA allocates something. ``is_global`` does most of it: on every supported
    Python it is ``not is_private`` (and, for IPv4, outside RFC 6598 shared space,
    100.64.0.0/10 — carrier NAT and every Tailscale node), and the private set already
    holds loopback, link-local (the 169.254.169.254 metadata service), unique-local
    (fd00:ec2::254), unspecified and RFC 1918. Naming those again changed no verdict.

    What ``is_global`` lets through, the two named categories catch: multicast (224.0.0.0/4,
    ff00::/8), and the IETF-reserved IPv6 blocks the private set does not list, among them
    ``::/8``, which holds the IPv4-compatible ``::127.0.0.1`` and the NAT64 prefix
    ``64:ff9b::/96`` (``64:ff9b::7f00:1`` carries 127.0.0.1). An IPv4-mapped IPv6 literal
    (``::ffff:100.64.0.1``) is judged as the IPv4 address it carries, since that is where
    the connection lands.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_reserved or ip.is_multicast)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Scheme, host and port, the port filled in when the URL leaves it implicit: httpx
    drops a default port when it prints a request URL. ``urlsplit`` lower-cases the
    scheme and ``hostname`` the host."""
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(parts.scheme)


def operator_configured(url: str) -> dict[str, Any]:
    """Request extensions marking a request to ``url`` as one the operator configured, so
    ``enforce_on_request`` passes it while it stays on ``url``'s origin. For the model
    endpoints only, never for a URL that came from a record or a caller."""
    return {_OPERATOR_CONFIGURED: _origin(url)}


def split_url(url: str, *, what: str) -> SplitResult:
    """``urlsplit`` for a URL taken from a record, refusing one that does not parse.

    The stdlib raises a bare ``ValueError`` for an unclosed IPv6 bracket (``urlsplit``) and
    for an out-of-range port (``.port``, lazily). Record URLs are uploader-controlled, so
    both reach here; they are refused with this module's ``ValidationError``, naming the
    file and the URL like every other refusal, instead of "Invalid IPv6 URL" with neither.
    Callers that gate on the scheme parse through this too, since they run first.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port  # validated here, not left to raise later at a random call site
    except ValueError as exc:
        raise ValidationError(f"{what}: malformed URL ({exc}); refusing to fetch {url}") from None
    return parts


def _target(url: str, what: str) -> str | None:
    """Host to check, or None when there is nothing to check.

    The host alone: the verdict is about the addresses a name resolves to, which do not
    depend on the port, so neither the lookup nor the approval cache takes one."""
    if os.environ.get(ALLOW_PRIVATE_ENV) == "1":
        return None

    parts = split_url(url, what=what)
    if parts.scheme not in ("http", "https"):
        # Scheme policy belongs to the callers, which gate before calling this; a file://
        # URL has no host to resolve and must not be reported as an egress problem.
        return None

    host = parts.hostname
    if not host:
        raise ValidationError(f"{what}: URL has no host; refusing to fetch {url}")
    return host


async def assert_public_url(url: str, *, what: str) -> None:
    """Raise ``ValidationError`` unless every address *url* resolves to is public.

    ``what`` names the thing being fetched, so the error says which file was refused.

    Rejects if ANY resolved address is non-public, not merely if all are: a name that
    answers with one public and one loopback address is exactly the shape an attacker
    would choose.

    A name that does not resolve is ALLOWED through. That reads like failing open and is
    not: the risk being controlled is "this name points somewhere private", and a name with
    no address points nowhere — the request cannot reach anything and fails at connect time
    with an accurate transport error. Refusing here would block every offline and mocked
    caller in exchange for closing nothing, since the address it would be protecting
    against does not exist.

    ASYNC because DNS blocks. Both call sites sit on the download path, and a synchronous
    ``socket.getaddrinfo`` there stalls the whole event loop — which silently serialized
    parallel fetches until ``test_fetch_parallel_overlaps_in_time`` caught it. The loop's
    own resolver keeps the guard off the critical path.
    """
    host = _target(url, what)
    if host is None:
        return

    loop = asyncio.get_running_loop()
    now = loop.time()
    approved_at = _approved.get(host)
    if approved_at is not None and now - approved_at < _APPROVAL_TTL_S:
        return

    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # no address == nowhere to reach; see the docstring
    except UnicodeError as exc:
        # The resolver IDNA-encodes the name first, and a name with an empty label
        # (``a..b``, ``.example.org``) or one over 63 octets cannot be encoded: it is not
        # a hostname at all. Refused like split_url's malformed URLs, naming file and URL,
        # instead of a bare "'idna' codec can't encode ..." that names neither.
        raise ValidationError(f"{what}: malformed URL ({exc}); refusing to fetch {url}") from None

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # getaddrinfo returns literals; pinned with a stubbed resolver
            raise ValidationError(
                f"{what}: {host!r} resolved to an unparseable address {addr!r}; "
                f"refusing to fetch {url}"
            ) from None
        if not _is_public(ip):
            raise ValidationError(
                f"{what}: {host!r} resolves to the non-public address {ip}; refusing to "
                f"fetch {url}. A record pointing into private address space would make "
                f"this server read something the caller cannot reach itself. Set "
                f"{ALLOW_PRIVATE_ENV}=1 only if you deliberately serve records from there."
            )

    # Cached only after every address passed, so a rejection is never remembered as an
    # approval — and re-checked from scratch once the short TTL lapses.
    _approved[host] = now


async def enforce_on_request(request: Any) -> None:
    """httpx request event hook: validate EVERY hop, redirects included.

    Checking the URL a caller hands us is not enough, and the gap is not subtle: the client
    follows redirects, so a record with a perfectly public URL that 302s to
    ``http://127.0.0.1:9200/`` reaches it with the call-site check satisfied. Measured
    before this existed — the guard was consulted once, about the entry URL, while the
    redirect target was fetched unchecked.

    A request hook is the only layer that sees every address actually connected to, which
    is what the control has to bind to. The call-site checks stay: they fail before any I/O
    and name the file, which a transport-level error cannot.

    Typed ``Any`` rather than ``httpx.Request`` so this module does not import httpx purely
    for an annotation; httpx passes the request object positionally.

    A request marked ``operator_configured`` is passed while it is addressed to the origin
    the mark names; on any other origin, a redirect target included, it is checked.
    """
    url = str(request.url)
    if request.extensions.get(_OPERATOR_CONFIGURED) == _origin(url):
        return
    await assert_public_url(url, what="request")


def enforce_on_request_sync(request: Any) -> None:
    """The same per-hop check, as a synchronous ``httpx.Client`` request hook.

    ``operate`` reads its source in worker threads (pyarrow and the CSV sniff are
    synchronous), so ``sourceio`` gives them a sync client. A worker thread has no running
    loop, so the async check runs on a loop of its own here. Called from a thread that
    already runs a loop it raises rather than skip the check.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(assert_public_url(str(request.url), what="request"))
        return
    raise RuntimeError(
        "enforce_on_request_sync called on a running event loop; hook an async client"
    )
