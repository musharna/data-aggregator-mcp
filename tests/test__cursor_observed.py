"""Cursor codec behaviour the nightly mutation run showed no test observed (#88).

The exact token a state encodes to, and the exact refusal for each malformed state,
each next to a well-formed state the same check lets through.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from data_aggregator_mcp import _cursor
from data_aggregator_mcp.errors import ValidationError

_OK = {"q": "rna", "size": 3, "offsets": {"zenodo": 3}}
_PREFIX = "[ValidationError] invalid or corrupt cursor: "


def _raw(state: object) -> str:
    """A token built without the code under test: base64 of the compact, sorted JSON."""
    text = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(text.encode()).decode()


def _refused(token: str, message: str) -> None:
    with pytest.raises(ValidationError, match="^" + re.escape(_PREFIX + message) + "$"):
        _cursor.decode(token)


def test_a_state_encodes_to_compact_sorted_json():
    """Compact and key-sorted: the same state always gives the same, shortest token,
    whatever order its keys were built in."""
    token = _cursor.encode({"b": 1, "a": [1, 2]})
    assert token == "eyJhIjpbMSwyXSwiYiI6MX0="
    assert base64.urlsafe_b64decode(token) == b'{"a":[1,2],"b":1}'
    assert _cursor.encode({"a": [1, 2], "b": 1}) == token


def test_a_non_ascii_state_round_trips():
    state = {**_OK, "q": "Arabidopsis thaliana – Wurzel ü"}
    assert _cursor.decode(_cursor.encode(state)) == state


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("YWJj", "Expecting value: line 1 column 1 (char 0)"),  # base64 of 'abc'
        ("abc", "Incorrect padding"),
        ("abéc", "string argument should contain only ASCII characters"),
        (_raw(_OK)[:-8], "Unterminated string starting at: line 1 column 35 (char 34)"),
    ],
    ids=["not-json", "truncated-base64", "non-ascii", "truncated-json"],
)
def test_an_undecodable_token_is_refused_with_the_reason(token, message):
    assert _cursor.decode(_raw(_OK)) == _OK
    _refused(token, message)


@pytest.mark.parametrize(
    "state",
    [["q", "size", "offsets"], {"q": "x", "size": 1}, "q size offsets", None],
    ids=["list-of-the-names", "missing-offsets", "string", "null"],
)
def test_a_state_without_the_required_fields_is_refused(state):
    """A JSON list holding the three names passes a bare subset test; it is not a state."""
    assert _cursor.decode(_raw(_OK)) == _OK
    _refused(_raw(state), "missing required fields")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"q": None}, "'q' must be a string"),
        ({"offsets": []}, "'offsets' must be a dict"),
        ({"size": True}, "'size' must be a positive integer"),
        ({"size": 0}, "'size' must be a positive integer"),
        ({"offsets": {"zenodo": -1}}, "offsets must be non-negative integers"),
        ({"offsets": {"zenodo": False}}, "offsets must be non-negative integers"),
        ({"ahead": []}, "'ahead' must map streams to non-negative integer lists"),
        ({"ahead": {"zenodo": [-1]}}, "'ahead' must map streams to non-negative integer lists"),
        ({"ahead": {"zenodo": 1}}, "'ahead' must map streams to non-negative integer lists"),
        ({"eq": None}, "'eq' must be a string"),
        ({"sources": "zenodo"}, "'sources' must be a list of strings"),
        ({"sources": ["zenodo", 1]}, "'sources' must be a list of strings"),
        ({"variants": "ab"}, "'variants' must be a list of 1 to 4 strings"),
        ({"variants": ["a", 1]}, "'variants' must be a list of 1 to 4 strings"),
    ],
)
def test_each_wrong_field_is_refused_with_its_own_message(change, message):
    assert _cursor.decode(_raw(_OK)) == _OK
    _refused(_raw({**_OK, **change}), message)


@pytest.mark.parametrize(
    "raw_variants",
    ["ab", ["a", 1], ["a"], ["a", "b", "c"]],
    ids=["a-string-of-the-right-length", "a-non-string", "too-few", "too-many"],
)
def test_raw_variants_must_be_strings_matching_the_variants(raw_variants):
    good = {**_OK, "variants": ["a", "b"], "raw_variants": ["A", "B"]}
    assert _cursor.decode(_raw(good)) == good
    _refused(
        _raw({**good, "raw_variants": raw_variants}),
        "'raw_variants' must be strings matching 'variants'",
    )


def test_raw_variants_without_variants_must_be_empty():
    """A cursor with no variants has no raw variants to match."""
    assert _cursor.decode(_raw({**_OK, "raw_variants": []})) == {**_OK, "raw_variants": []}
    _refused(
        _raw({**_OK, "raw_variants": ["a"]}), "'raw_variants' must be strings matching 'variants'"
    )


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"kind": 1}, "'kind' must be one of "),
        ({"published_after": 1.5}, "year filters must be integers"),
        ({"published_before": "1"}, "year filters must be integers"),
        ({"rank": "x"}, "'filters' may hold only published_after, published_before and kind"),
        ("x", "'filters' may hold only published_after, published_before and kind"),
    ],
)
def test_each_wrong_filter_is_refused_with_its_own_message(filters, message):
    good = {**_OK, "filters": {"published_after": 2000, "published_before": 2020, "kind": "study"}}
    assert _cursor.decode(_raw(good)) == good
    with pytest.raises(ValidationError, match="^" + re.escape(_PREFIX + message)):
        _cursor.decode(_raw({**_OK, "filters": filters}))


def test_the_kind_refusal_lists_every_kind_a_search_accepts():
    _refused(
        _raw({**_OK, "filters": {"kind": "other"}}),
        "'kind' must be one of ['dataset', 'publication', 'sequencing_run', 'software', 'study']",
    )
    for kind in ("dataset", "publication", "sequencing_run", "software", "study"):
        state = {**_OK, "filters": {"kind": kind}}
        assert _cursor.decode(_raw(state)) == state


def test_the_variant_count_is_bounded_on_both_sides():
    for n in (1, 4):
        state = {**_OK, "variants": ["v"] * n}
        assert _cursor.decode(_raw(state)) == state
    for n in (0, 5):
        _refused(
            _raw({**_OK, "variants": ["v"] * n}), "'variants' must be a list of 1 to 4 strings"
        )
