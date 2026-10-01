"""Shared HTTP retry helper: transport + 429/5xx + malformed-body retry, status→typed-error."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from defusedxml import ElementTree as ET  # remote XML: entity-expansion safe

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp.errors import (
    NotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
)

_RAISE = object()


def doi_path(doi: str) -> str:
    """``doi`` as a URL path (``/`` kept, everything else reserved percent-encoded).

    DOIs may legally contain ``#``, ``?``, ``;`` and ``<>`` (SICI DOIs do). Interpolated
    raw into a URL, ``#`` ends the path as a fragment and ``?`` starts a query, so the
    upstream is asked about a DIFFERENT DOI. Every DOI-in-path request goes through here.
    """
    return quote(doi)  # quote() keeps "/" and nothing else reserved by default


_RETRY_AFTER_CAP = 60.0
# httpx headers are case-insensitive, so any spelling of the name reads the same header.
_RETRY_AFTER = "Retry-After"
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
# 2xx statuses that carry no body by definition.
_NO_CONTENT_STATUSES = (204, 205)
# Redirect statuses returned (not followed) when a caller passes follow_redirects=False
# — e.g. DataONE /resolve/ answers 303 with the Member-Node url in the Location header.
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# Transport-level failures (no HTTP response): connect/read/write/timeout/protocol.
_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.TransportError)
# Malformed 2xx body: json.JSONDecodeError ⊂ ValueError; ET.ParseError ⊄ ValueError.
_PARSE_ERRORS = (ValueError, ET.ParseError)
_DELAY_SECONDS_RE = re.compile(r"[0-9]+")


def _retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds a ``Retry-After`` header asks the client to wait (RFC 9110 §10.2.3): a
    non-negative integer, or an HTTP-date (the seconds from ``now`` until it; 0 once it
    has passed). None when the header is absent or is neither, so the caller uses its
    own backoff. ``float()`` parsed it before 2026-09-30: it read an HTTP-date as
    invalid, and it accepted ``nan``, which made ``asyncio.sleep`` raise ``ValueError``."""
    if value is None:
        return None
    value = value.strip()
    if _DELAY_SECONDS_RE.fullmatch(value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except ValueError:
        return None
    if when.tzinfo is None:  # the asctime form carries no zone; every HTTP-date is GMT
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


async def _retrying(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    content: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    max_retries: int,
    not_found_returns: Any = _RAISE,
    parse: Callable[[httpx.Response], Any] | None = None,
    follow_redirects: bool = True,
    no_content_returns: Any = _RAISE,
    empty_answer: Callable[[httpx.Response], bool] | None = None,
) -> Any:
    """Issue ``method url`` with retry + classification. Transport errors and
    (when ``parse`` is given) a malformed 2xx body are retried like a 5xx, then
    raise ``UpstreamUnavailableError`` on terminal failure. Returns ``parse(resp)``
    when ``parse`` is given, else the 2xx ``Response``. ``not_found_returns=<x>``
    returns ``<x>`` on 404 instead of raising.

    Every 2xx is a success, not just 200: RCSB answers a zero-hit search with
    ``204 No Content``, and counting that as a failure turned "no hits" into a
    retried-then-reported outage. A body-less 2xx (204/205) returns
    ``no_content_returns=<x>`` when the caller declared what "no content" means for
    that endpoint; with ``parse`` and no declaration it raises (an empty body cannot
    be parsed, and guessing an empty result is exactly the silent failure to avoid).

    ``empty_answer(resp)`` lets an endpoint that encodes "zero hits" as an error status
    say so precisely (OpenML: ``412`` with error code ``372``); a matching response
    returns ``no_content_returns``, every other error status is classified as usual.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be at least 1, got {max_retries}")
    delay = 1.0
    attempt = 0
    while True:
        attempt += 1
        retry = attempt < max_retries
        try:
            await _ratelimit.acquire(service, url)
            resp = await client.request(
                method,
                url,
                params=params,
                data=data,
                content=content,
                headers=headers,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
        except _TRANSPORT_ERRORS as exc:
            if retry:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise UpstreamUnavailableError(
                f"{service} unreachable after {max_retries} tries: {exc!r}"
            ) from exc

        if resp.status_code in _NO_CONTENT_STATUSES and parse is not None:
            if no_content_returns is not _RAISE:
                return no_content_returns
            raise UpstreamUnavailableError(
                f"{service} → HTTP {resp.status_code} (no content) where a body was expected"
            )
        if 200 <= resp.status_code < 300 or (
            not follow_redirects and resp.status_code in _REDIRECT_STATUSES
        ):
            if parse is None:
                return resp
            try:
                return parse(resp)
            except _PARSE_ERRORS as exc:
                if retry:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise UpstreamUnavailableError(
                    f"{service} returned an unparseable 200 body after {max_retries} tries: {exc!r}"
                ) from exc
        # Every 2xx has returned, retried or raised above: what follows is an error status.
        if empty_answer is not None and no_content_returns is not _RAISE and empty_answer(resp):
            return no_content_returns
        if resp.status_code == 404:
            if not_found_returns is not _RAISE:
                return not_found_returns
            raise NotFoundError(f"{service} → HTTP 404: {resp.text[:200]}")
        if resp.status_code in _RETRYABLE_STATUSES:
            if retry:
                asked = _retry_after(resp.headers.get(_RETRY_AFTER), datetime.now(UTC))
                await asyncio.sleep(min(delay if asked is None else asked, _RETRY_AFTER_CAP))
                delay *= 2
                continue
            if resp.status_code == 429:
                raise RateLimitError(f"{service} exhausted {max_retries} retries (HTTP 429)")
            raise UpstreamUnavailableError(
                f"{service} exhausted {max_retries} retries (last HTTP {resp.status_code})"
            )
        raise UpstreamUnavailableError(f"{service} → HTTP {resp.status_code}: {resp.text[:200]}")


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    not_found_returns: Any = _RAISE,
    follow_redirects: bool = True,
) -> httpx.Response | Any:
    """Return the 2xx ``Response``; transport / 429 / 5xx retried; terminal → typed error.
    Pass ``not_found_returns=<sentinel>`` to return the sentinel on 404. Pass
    ``follow_redirects=False`` to return a 3xx ``Response`` unfollowed (read its
    ``Location`` header) instead of chasing it."""
    return await _retrying(
        client,
        method,
        url,
        service=service,
        params=params,
        data=data,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        not_found_returns=not_found_returns,
        follow_redirects=follow_redirects,
    )


def _validate_xml(resp: httpx.Response) -> httpx.Response:
    ET.fromstring(resp.text)  # raises ET.ParseError on a truncated/garbage body
    return resp


async def request_xml(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    not_found_returns: Any = _RAISE,
) -> httpx.Response | Any:
    """Return the Response after confirming its body parses as XML (``ET.ParseError``
    retried, then ``UpstreamUnavailableError``). Callers read ``.text``."""
    return await _retrying(
        client,
        method,
        url,
        service=service,
        params=params,
        data=data,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        not_found_returns=not_found_returns,
        parse=_validate_xml,
    )


class UnexpectedShapeError(ValueError):
    """A 2xx JSON body whose top-level type is not what the endpoint promises.
    A ``ValueError`` so ``_retrying`` treats it like any other malformed body."""


class UpstreamEnvelopeError(ValueError):
    """A 2xx body that parsed fine but carries the upstream's own error envelope."""


def _json_parser(
    expect: type | tuple[type, ...], check: Callable[[Any], None] | None
) -> Callable[[httpx.Response], Any]:
    names = expect.__name__ if isinstance(expect, type) else " or ".join(t.__name__ for t in expect)

    def parse(resp: httpx.Response) -> Any:
        body = resp.json()
        if not isinstance(body, expect):
            raise UnexpectedShapeError(
                f"expected {names} JSON, got {type(body).__name__}: {resp.text[:200]}"
            )
        if check is not None:
            check(body)
        return body

    return parse


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    expect: type | tuple[type, ...],
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    content: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    not_found_returns: Any = _RAISE,
    no_content_returns: Any = _RAISE,
    empty_answer: Callable[[httpx.Response], bool] | None = None,
    check: Callable[[Any], None] | None = None,
) -> Any:
    """Return the parsed JSON body. A malformed 200 body (NCBI throttle envelope)
    is retried, then raises ``UpstreamUnavailableError``.

    ``expect`` is REQUIRED: the JSON top-level type the endpoint promises (``dict``,
    ``list``, a tuple of types, or ``object`` for an endpoint that may answer any JSON).
    A 200 carrying anything else — typically ``null``, ``[]`` or an error envelope such
    as ``{"detail": "Internal error"}`` where a list was promised — is a malformed body
    (retried, then ``UpstreamUnavailableError``), never coerced into "no results" or
    "not found". It was optional until 2026-09-30, and 37 of 50 call sites left it out;
    a caller's ``(body or {})`` then read those bodies as an empty answer.

    ``check(body)`` inspects the parsed body for an error the upstream smuggles inside
    a 200 (NCBI's ``esearchresult.ERROR``). It raises ``UpstreamEnvelopeError`` (a
    ``ValueError``) to have the body treated as malformed: retried, then raised.
    """
    parse = _json_parser(expect, check)
    return await _retrying(
        client,
        method,
        url,
        service=service,
        params=params,
        data=data,
        content=content,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        not_found_returns=not_found_returns,
        parse=parse,
        no_content_returns=no_content_returns,
        empty_answer=empty_answer,
    )


async def request_json_with_headers(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    expect: type | tuple[type, ...],
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> tuple[Any, httpx.Headers]:
    """``request_json`` for an endpoint whose answer is split between the body and a
    header (UniProt's hit count is ``x-total-results``): the body, checked against
    ``expect`` and retried exactly as ``request_json`` does, and the response headers.
    Reading ``resp.json()`` off ``request_with_retry`` instead skips both."""
    parse = _json_parser(expect, None)
    return await _retrying(
        client,
        method,
        url,
        service=service,
        params=params,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        parse=lambda resp: (parse(resp), resp.headers),
    )
