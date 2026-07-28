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

KNOWN LIMITATION — this does not defeat DNS rebinding. The name is resolved here and
resolved again by the HTTP client, so a server that answers public-then-private between
those two lookups still wins. Closing that requires pinning the checked address into the
connection itself. This raises the bar from "trivially exploitable by anyone who can upload
a record" to "needs a rebinding-capable resolver", which is worth having while being honest
that it is not total.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlsplit

from data_aggregator_mcp.errors import ValidationError

#: Operators who genuinely serve records from private address space — an on-prem mirror, a
#: dev fixture — set this to "1". Named for what it permits, so it cannot be mistaken for a
#: performance knob.
ALLOW_PRIVATE_ENV = "DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS"

#: Seconds an approved (host, port) stays approved. Files in one record almost always share
#: a host, so without this a 4-file fetch pays four resolutions of the same name — enough to
#: measurably serialize a parallel download. Deliberately SHORT, and only successful
#: verdicts are cached: a longer window would widen the DNS-rebinding gap the module
#: docstring already declines to defend against, and there is no reason to also make it
#: last minutes.
_APPROVAL_TTL_S = 30.0
_approved: dict[tuple[str, int], float] = {}


def _clear_cache() -> None:
    """Drop cached approvals. For tests that flip the env var between assertions."""
    _approved.clear()


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for addresses that are routable on the public internet.

    Deliberately a denylist of *categories* rather than of specific ranges: link-local
    covers the cloud metadata services (169.254.169.254 and fd00:ec2::254), and reserved
    covers the blocks that get repurposed later. A range-by-range list would need editing
    every time IANA allocates something.
    """
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _target(url: str, what: str) -> tuple[str, int] | None:
    """Host and port to check, or None when there is nothing to check."""
    if os.environ.get(ALLOW_PRIVATE_ENV) == "1":
        return None

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        # Scheme policy belongs to the callers, which gate before calling this; a file://
        # URL has no host to resolve and must not be reported as an egress problem.
        return None

    host = parts.hostname
    if not host:
        raise ValidationError(f"{what}: URL has no host; refusing to fetch {url}")
    return host, parts.port or (443 if parts.scheme == "https" else 80)


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
    target = _target(url, what)
    if target is None:
        return
    host, port = target

    loop = asyncio.get_running_loop()
    now = loop.time()
    approved_at = _approved.get(target)
    if approved_at is not None and now - approved_at < _APPROVAL_TTL_S:
        return

    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # no address == nowhere to reach; see the docstring

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover — getaddrinfo returns literals
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
    _approved[target] = now


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
    """
    await assert_public_url(str(request.url), what="request")
