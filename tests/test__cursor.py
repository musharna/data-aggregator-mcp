from __future__ import annotations

import pytest

from data_aggregator_mcp import _cursor
from data_aggregator_mcp.errors import ValidationError


def test_cursor_roundtrip():
    state = {
        "q": "rice drought",
        "sources": ["zenodo", "datacite"],
        "organism": None,
        "filters": {"published_after": 2015, "published_before": None, "kind": "dataset"},
        "size": 10,
        "offsets": {"zenodo": 10, "datacite": 5},
    }
    token = _cursor.encode(state)
    assert isinstance(token, str)
    assert _cursor.decode(token) == state


def test_cursor_is_opaque_urlsafe():
    token = _cursor.encode({"q": "x", "size": 10, "offsets": {}})
    assert "/" not in token and "+" not in token  # urlsafe b64


@pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj", "{}"])
def test_cursor_decode_rejects_garbage(bad):
    with pytest.raises(ValidationError):
        _cursor.decode(bad)


def _encode_raw(state: dict) -> str:
    """Encode a dict directly without going through the production helpers."""
    import base64
    import json

    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def test_cursor_decode_rejects_variants_not_list():
    """variants must be a list of str when present; a string value must be rejected."""
    token = _encode_raw({"q": "x", "size": 10, "offsets": {}, "variants": "XX"})
    with pytest.raises(ValidationError, match="invalid or corrupt cursor"):
        _cursor.decode(token)


def test_cursor_decode_rejects_offsets_not_dict():
    """offsets must be a dict."""
    token = _encode_raw({"q": "x", "size": 10, "offsets": [1, 2, 3]})
    with pytest.raises(ValidationError, match="invalid or corrupt cursor"):
        _cursor.decode(token)


def test_cursor_decode_rejects_size_not_positive_int():
    """size must be a positive integer (> 0)."""
    for bad_size in [0, -1, "ten", 1.5]:
        token = _encode_raw({"q": "x", "size": bad_size, "offsets": {}})
        with pytest.raises(ValidationError, match="invalid or corrupt cursor"):
            _cursor.decode(token)
