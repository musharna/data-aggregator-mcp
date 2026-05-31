"""Opaque, URL-safe pagination cursor: base64(json(state)). The token is never
promised to callers — only that a ``next_cursor`` from one search is replayable."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ValidationError

_REQUIRED = {"q", "size", "offsets"}


def encode(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        state = json.loads(raw)
    except Exception as exc:
        raise ValidationError(f"invalid or corrupt cursor: {exc}") from exc
    if not isinstance(state, dict) or not _REQUIRED.issubset(state):
        raise ValidationError("invalid or corrupt cursor: missing required fields")
    return state
