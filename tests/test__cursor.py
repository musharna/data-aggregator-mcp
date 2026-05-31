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
