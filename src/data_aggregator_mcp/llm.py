"""Optional NL→structured query understanding via a remote OpenAI-compatible chat endpoint.

Disabled (returns None) unless ``LLM_API_BASE`` is set. An unreachable endpoint, an error
status or a malformed answer never raises into the search path: it is logged and degrades
to the raw keyword query. No local model, no required key (a keyless local server is
supported by omitting the auth header).

An answer is read only if its first choice carries a message (``_check_answer``); anything
else is malformed: retried, then skipped. The message content must be a string holding a
JSON object; a model that answers otherwise is skipped without a retry, since the same
prompt at temperature 0 gets the same answer.

NOTE: ``_http`` has no ``json=`` param, so we serialize the JSON ourselves, pass it as
``content=`` (httpx's non-deprecated raw-body param), and set Content-Type explicitly.
The ``response_format={"type": "json_object"}`` hint stabilizes JSON output; endpoints
that ignore it still work as long as the model answers with bare JSON.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"
# Module constants are not mutated: httpx upper-cases the method, and header names are
# case-insensitive, so a case mutant of either is equivalent. test_llm_observed pins the
# request each one builds.
_METHOD = "POST"
_JSON_CONTENT = {"Content-Type": "application/json"}
_AUTHORIZATION = "Authorization"
# RFC 6750 §2.1 b64token. Anything else cannot go in the header: httpx raises a bare
# UnicodeEncodeError for a non-ASCII key, and a line break (a CRLF .env file) is an h11
# protocol error, retried as if the network had failed, whose text quotes the header,
# key included.
_BEARER_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_SKIPPED = "query understanding skipped: %s"
_SCHEMES = ("http", "https")


def _config() -> tuple[str, str | None, str] | None:
    base = os.environ.get("LLM_API_BASE")
    if not base:
        return None
    return (
        base.rstrip("/"),
        os.environ.get("LLM_API_KEY"),
        os.environ.get("LLM_MODEL", _DEFAULT_MODEL),
    )


def _is_http_url(url: str) -> bool:
    """Whether httpx can send to ``url``: an http(s) URL with a host. A URL it cannot
    parse raises ``InvalidURL``, and a malformed IDNA host (``xn--``) raises ``idna``'s
    ``ValueError`` when the host is read; neither is a ``TransportError``. A missing
    scheme or host is one, retried as if the network had failed."""
    try:
        parsed = httpx.URL(url)
        return parsed.scheme in _SCHEMES and bool(parsed.host)
    except (httpx.InvalidURL, ValueError):
        return False


def _check_answer(body: dict) -> None:
    """A ``request_json`` check: a non-empty ``choices`` list whose first entry is an
    object with a ``message`` object. The content inside it is the model's, read after."""
    choices = body.get("choices")
    if not (
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
        and isinstance(choices[0].get("message"), dict)
    ):
        raise _http.UpstreamEnvelopeError(f"no chat message in {body!r:.200}")


def _parse_object(content: object) -> dict[str, Any] | None:
    """The message content parsed as a JSON object, or None (logged) if it is not one."""
    if not isinstance(content, str):
        logger.warning(_SKIPPED, f"the message content is not text: {content!r:.200}")
        return None
    try:
        parsed = json.loads(content)
    # RecursionError: deeply nested JSON exhausts the parser's stack.
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict):
        logger.warning(_SKIPPED, f"the message content is not a JSON object: {content!r:.200}")
        return None
    return parsed


async def complete_json(
    client: httpx.AsyncClient, *, system: str, user: str
) -> dict[str, Any] | None:
    """POST a system+user prompt to an OpenAI-compatible ``/chat/completions`` endpoint
    and return the assistant message content parsed as a JSON object.

    Returns None when no endpoint is configured, when ``LLM_API_BASE`` is not an http(s)
    URL or ``LLM_API_KEY`` is not a bearer token, when the endpoint fails or answers
    without a message, or when the message is not a JSON object. Each of these but the
    first is logged as a warning, never with the key in it. NEVER raises into the search
    path (same fail-soft discipline as ``embeddings.embed``)."""
    cfg = _config()
    if cfg is None:
        return None
    base, key, model = cfg
    url = f"{base}/chat/completions"
    if not _is_http_url(url):
        logger.warning(_SKIPPED, "LLM_API_BASE is not an http(s) URL")
        return None
    if key and not _BEARER_TOKEN.fullmatch(key):
        logger.warning(_SKIPPED, "LLM_API_KEY is not a bearer token")
        return None
    headers = dict(_JSON_CONTENT)
    if key:
        headers[_AUTHORIZATION] = f"Bearer {key}"
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    )
    try:
        body = await _http.request_json(
            client,
            _METHOD,
            url,
            service="llm",
            content=payload,
            headers=headers,
            expect=dict,
            check=_check_answer,
        )
    except DataAggregatorError as exc:
        # An error status quotes the answer's body, which a server may echo the key into.
        reason = str(exc).replace(key, "<LLM_API_KEY>") if key else str(exc)
        logger.warning(_SKIPPED, reason)
        return None
    return _parse_object(body["choices"][0]["message"].get("content"))
