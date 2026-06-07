"""Shared HTTP retry helper: transport + 429/5xx + malformed-body retry, status→typed-error."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp.errors import (
    NotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
)

_RAISE = object()
_RETRY_AFTER_CAP = 60.0
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
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
) -> Any:
    """Issue ``method url`` with retry + classification. Transport errors and
    (when ``parse`` is given) a malformed 2xx body are retried like a 5xx, then
    raise ``UpstreamUnavailableError`` on terminal failure. Returns ``parse(resp)``
    when ``parse`` is given, else the 2xx ``Response``. ``not_found_returns=<x>``
    returns ``<x>`` on 404 instead of raising.
    """
    delay = 1.0
    last_status: int | None = None
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            await _ratelimit.acquire(service)
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

        if resp.status_code == 200 or (
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
) -> Any:
    """Return the parsed JSON body. A malformed 200 body (NCBI throttle envelope)
    is retried, then raises ``UpstreamUnavailableError``."""
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
        parse=_parse_json,
    )
