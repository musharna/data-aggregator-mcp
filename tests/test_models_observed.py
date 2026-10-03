"""Pin the exact behaviour of the ``models`` helpers every adapter normalizes through."""

from __future__ import annotations

import pytest

from data_aggregator_mcp.models import (
    DataResource,
    FileEntry,
    Link,
    _orcid,
    _rel,
    compact,
    derive_access_modes,
    derive_version_status,
    local_id,
    normalize_access,
    strip_html,
    year_from,
)


@pytest.mark.parametrize(
    ("raw", "bare"),
    [
        ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("http://orcid.org/a/b/0000-0002-1825-0097", "0000-0002-1825-0097"),
        (" 0000-0002-1825-009x ", "0000-0002-1825-009X"),
        ("https://orcid.org/ 0000-0002-1825-009x", "0000-0002-1825-009X"),
        ("0000-0002-1825-0097-extra", None),
        ("x0000-0002-1825-0097", None),
        ("0000-0002-1825-009Y", None),
        ("0000-0002-1825-0097/", None),
        ("", None),
        (None, None),
    ],
)
def test_orcid_is_the_last_path_segment_trimmed_and_upper_cased(raw, bare) -> None:
    assert _orcid(raw) == bare


@pytest.mark.parametrize(
    ("raw", "snake"),
    [
        ("IsSupplementTo", "is_supplement_to"),
        ("isPartOf", "is_part_of"),
        ("References", "references"),
        ("HasPart", "has_part"),
    ],
)
def test_rel_snake_cases_a_relation_type(raw, snake) -> None:
    assert _rel(raw) == snake


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("open", "open"),
        (" OPEN ", "open"),
        ("Embargo", "embargoed"),
        ("embargoed", "embargoed"),
        ("restricted", "restricted"),
        ("closed", "closed"),
        ("metadataonly", "unknown"),
        (True, "unknown"),
        ("   ", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_access(raw, norm) -> None:
    assert normalize_access(raw) == norm


def test_local_id_defaults_neither_strip_nor_upper_case() -> None:
    assert local_id("pdb: 1abc ", "pdb") == " 1abc "
    assert local_id("pdb: 1abc ", "pdb", upper=True) == " 1ABC "
    assert local_id("pdb:a:b", "pdb") == "a:b"
    assert local_id("pdbx:1", "pdb") == "pdbx:1"  # a different prefix is not this one


@pytest.mark.parametrize(
    ("vals", "year"),
    [
        (("2019",), 2019),
        (("20x9", "1999-12"), 1999),
        (("", None, "1850-01"), 1850),
        ((123, "2001"), 2001),
        (("201",), None),
        ((), None),
    ],
)
def test_year_from(vals, year) -> None:
    assert year_from(*vals) == year


def test_strip_html_replaces_tags_with_a_space() -> None:
    assert strip_html("a<br>b") == "a b"
    assert strip_html("<p></p>") is None
    assert strip_html("") is None


def test_derive_version_status_takes_the_first_newer_version() -> None:
    links = [
        Link(rel="is_version_of", target_id="c"),
        Link(rel="is_obsoleted_by", target_id="n1"),
        Link(rel="is_previous_version_of", target_id="n2"),
    ]
    assert derive_version_status(links) == (False, "n1")
    assert derive_version_status(links[2:]) == (False, "n2")
    assert derive_version_status(links[:1]) == (None, None)
    assert derive_version_status([]) == (None, None)


@pytest.mark.parametrize(
    ("files", "operate", "fetchable", "modes"),
    [
        (
            [FileEntry(name="a.CSV", url="u")],
            True,
            True,
            ["fetch", "schema", "preview", "head", "sql"],
        ),
        (
            [FileEntry(name="a.pq", url="u")],
            True,
            True,
            ["fetch", "schema", "preview", "head", "sql"],
        ),
        ([FileEntry(name="a.tsv", url="u")], False, True, ["fetch"]),
        ([FileEntry(name="a.csv", url="u")], True, False, []),
        ([FileEntry(name="a.csv.gz", url="u")], True, True, ["fetch"]),
        ([FileEntry(name="a.csv"), FileEntry(name="b.txt", url="u")], True, True, ["fetch"]),
        ([FileEntry(name="a.csv", url="")], True, True, []),
        ([], True, True, []),
    ],
)
def test_derive_access_modes(files, operate, fetchable, modes) -> None:
    assert derive_access_modes(files, operate=operate, fetchable=fetchable) == modes


def test_compact_keeps_a_description_at_the_limit_and_cuts_one_over() -> None:
    r = DataResource(id="x:1", source="x", kind="dataset", title="t", description="d" * 500)
    assert compact(r).description == "d" * 500
    longer = r.model_copy(update={"description": "d" * 500 + "e"})
    assert compact(longer).description == "d" * 500
    assert compact(r.model_copy(update={"description": ""})).description is None
