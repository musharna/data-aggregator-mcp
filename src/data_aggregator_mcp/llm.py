"""Optional NL→structured query understanding via a remote OpenAI-compatible chat endpoint.

Disabled (returns None) unless ``LLM_API_BASE`` is set. NEVER raises into the search
path — any failure degrades to the raw keyword query. No local model, no required key
(a keyless local server is supported by omitting the auth header).

NOTE: ``_http`` has no ``json=`` param, so we serialize the JSON ourselves, pass it as
``content=`` (httpx's non-deprecated raw-body param), and set Content-Type explicitly.
The ``response_format={"type": "json_object"}`` hint stabilizes JSON output; endpoints
that ignore it still work because we parse the assistant message content defensively.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from data_aggregator_mcp import _http


def _config() -> tuple[str, str | None, str] | None:
    base = os.environ.get("LLM_API_BASE")
    if not base:
        return None
    return (
        base.rstrip("/"),
        os.environ.get("LLM_API_KEY"),
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )


async def complete_json(
    client: httpx.AsyncClient, *, system: str, user: str
) -> dict[str, Any] | None:
    """POST a system+user prompt to an OpenAI-compatible ``/chat/completions`` endpoint
    and return the assistant message content parsed as a JSON object.

    Returns None when no endpoint is configured, on ANY exception, or when the response
    cannot be parsed into a ``dict``. NEVER raises (same fail-soft discipline as
    ``embeddings.embed``)."""
    cfg = _config()
    if cfg is None:
        return None
    base, key, model = cfg
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
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
            "POST",
            f"{base}/chat/completions",
            service="llm",
            content=payload,
            headers=headers,
        )
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception:  # unavailable / malformed — caller degrades to raw query
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
