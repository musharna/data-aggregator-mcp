"""Shared HTTP retry helper: transport + 429/5xx + malformed-body retry, status→typed-error."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
    return quote(doi, safe="/")


_RETRY_AFTER_CAP = 60.0
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
    timeout: float = 30.0,
    max_retries: int = 3,
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
    delay = 1.0
    last_status: int | None = None
    last_exc: Exception | None = None
    for attempt in range(max_retries):
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
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise UpstreamUnavailableError(
                f"{service} unreachable after {max_retries} tries: {exc!r}"
            ) from exc

        last_status = resp.status_code

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
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise UpstreamUnavailableError(
                    f"{service} returned an unparseable 200 body after {max_retries} tries: {exc!r}"
                ) from exc
        if (
            empty_answer is not None
            and no_content_returns is not _RAISE
            and not 200 <= resp.status_code < 300
            and empty_answer(resp)
        ):
            return no_content_returns
        if resp.status_code == 404 and not_found_returns is not _RAISE:
            return not_found_returns
        if resp.status_code in _RETRYABLE_STATUSES:
            if attempt < max_retries - 1:
                hdr = resp.headers.get("Retry-After")
                try:
                    retry_after = float(hdr) if hdr else delay
                except ValueError:
                    retry_after = delay
                retry_after = min(retry_after, _RETRY_AFTER_CAP)
                await asyncio.sleep(retry_after)
                delay *= 2
                continue
            break
        if resp.status_code == 404:
            raise NotFoundError(f"{service} → HTTP 404: {resp.text[:200]}")
        if resp.status_code == 429:
            raise RateLimitError(f"{service} rate-limited (HTTP 429): {resp.text[:200]}")
        raise UpstreamUnavailableError(f"{service} → HTTP {resp.status_code}: {resp.text[:200]}")

    if last_status == 429:
        raise RateLimitError(f"{service} exhausted {max_retries} retries (HTTP 429)")
    if last_status is None:
        raise UpstreamUnavailableError(
            f"{service} unreachable after {max_retries} retries: {last_exc!r}"
        )
    raise UpstreamUnavailableError(
        f"{service} exhausted {max_retries} retries (last HTTP {last_status})"
    )


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
        parse=None,
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


def _parse_json(resp: httpx.Response) -> Any:
    return resp.json()


class UnexpectedShapeError(ValueError):
    """A 2xx JSON body whose top-level type is not what the endpoint promises.
    A ``ValueError`` so ``_retrying`` treats it like any other malformed body."""


class UpstreamEnvelopeError(ValueError):
    """A 2xx body that parsed fine but carries the upstream's own error envelope."""


def _json_parser(
    expect: type | tuple[type, ...] | None, check: Callable[[Any], None] | None
) -> Callable[[httpx.Response], Any]:
    if expect is None and check is None:
        return _parse_json
    if expect is None:
        names = ""
    elif isinstance(expect, type):
        names = expect.__name__
    else:
        names = " or ".join(t.__name__ for t in expect)

    def parse(resp: httpx.Response) -> Any:
        body = resp.json()
        if expect is not None and not isinstance(body, expect):
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
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    content: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    not_found_returns: Any = _RAISE,
    no_content_returns: Any = _RAISE,
    empty_answer: Callable[[httpx.Response], bool] | None = None,
    expect: type | tuple[type, ...] | None = None,
    check: Callable[[Any], None] | None = None,
) -> Any:
    """Return the parsed JSON body. A malformed 200 body (NCBI throttle envelope)
    is retried, then raises ``UpstreamUnavailableError``.

    ``expect`` is the JSON top-level type the endpoint promises (``list``/``dict``).
    A 200 carrying anything else — typically an error envelope such as
    ``{"detail": "Internal error"}`` where a list was promised — is a malformed body
    (retried, then ``UpstreamUnavailableError``), never coerced into "no results".

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
