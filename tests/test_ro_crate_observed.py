"""RO-Crate export: the graph must be flattened, as RO-Crate 1.1 requires.

Authors were nested in the root entity as ``{"@type": "Person", "name": ...}`` with no
``@id``. rocrate-validator 0.12.1 (profile ro-crate-1.1, check 3.3 "File Descriptor
JSON-LD must be flattened", severity REQUIRED) rejected 40 of 59 crates built from
live-resolved records (2026-10-02): every crate whose record had a creator. Each author
is now a Person entity of its own, identified by its ORCID iD when known.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from data_aggregator_mcp import ro_crate
from data_aggregator_mcp.models import Creator, DataResource, FileEntry

_DESCRIPTOR = {
    "@id": "ro-crate-metadata.json",
    "@type": "CreativeWork",
    "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
    "about": {"@id": "./"},
}


def _nested_objects(value: Any) -> list[dict[str, Any]]:
    """Every JSON object held anywhere inside a property value."""
    if isinstance(value, dict):
        return [value, *(o for v in value.values() for o in _nested_objects(v))]
    if isinstance(value, list):
        return [o for v in value for o in _nested_objects(v)]
    return []


def _flattening_violations(crate: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(entity, property, object) for each nested object that is not a bare reference.
    A flattened JSON-LD graph may nest only ``{"@id": ...}`` (validator check 3.3)."""
    out = []
    for ent in crate["@graph"]:
        for key, value in ent.items():
            for obj in _nested_objects(value):
                if set(obj) != {"@id"}:
                    out.append((ent["@id"], key, obj))
    return out


def test_authors_are_flattened_person_entities_with_orcid_ids() -> None:
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        creators=[
            Creator(name="Banda, Juan M.", orcid="0000-0001-8499-824X"),
            Creator(name="Wang, Guanyu"),
            Creator(name="Banda, J. M.", orcid="0000-0001-8499-824X"),
            Creator(name="Wang, Guanyu"),
        ],
    )
    crate = ro_crate.render(r)

    assert _flattening_violations(crate) == []
    assert crate["@graph"] == [
        _DESCRIPTOR,
        {
            "@id": "./",
            "@type": "Dataset",
            "name": "t",
            "author": [
                {"@id": "https://orcid.org/0000-0001-8499-824X"},
                {"@id": "#author-1"},
                {"@id": "#author-3"},
            ],
            "hasPart": [],
        },
        # One ORCID is one person: the second listing under it adds no entity.
        {
            "@id": "https://orcid.org/0000-0001-8499-824X",
            "@type": "Person",
            "name": "Banda, Juan M.",
        },
        # Without an ORCID, two equal names may be two people: each keeps its own id.
        {"@id": "#author-1", "@type": "Person", "name": "Wang, Guanyu"},
        {"@id": "#author-3", "@type": "Person", "name": "Wang, Guanyu"},
    ]


def test_every_reference_in_a_full_crate_resolves_to_an_entity() -> None:
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        creators=[Creator(name="A", orcid="0000-0002-1825-0097"), Creator(name="B")],
        files=[FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=1)],
    )
    crate = ro_crate.render(r)
    assert _flattening_violations(crate) == []

    ids = {e["@id"] for e in crate["@graph"]}
    refs = {
        o["@id"]
        for ent in crate["@graph"]
        for key, value in ent.items()
        if key != "conformsTo"  # names the RO-Crate spec, outside the crate
        for o in _nested_objects(value)
    }
    assert refs == {"./", "https://orcid.org/0000-0002-1825-0097", "#author-1", "https://x/a.csv"}
    assert refs <= ids


def test_the_violation_detector_sees_a_nested_node() -> None:
    """Positive control for ``_flattening_violations``: it flags the old author shape."""
    crate = {
        "@graph": [
            {"@id": "./", "author": [{"@type": "Person", "name": "A"}], "about": {"@id": "x"}}
        ]
    }
    assert _flattening_violations(crate) == [("./", "author", {"@type": "Person", "name": "A"})]


# --- live real-execution check ---------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_ro_crate_of_a_record_with_and_without_orcids_is_flattened() -> None:
    """zenodo:7834392 lists creators with an ORCID (Banda, Tekumalla) and without one
    (Wang): the crate resolved through the real server must be flattened and carry both
    kinds of Person id."""
    from data_aggregator_mcp import server

    out = await server._dispatch("resolve", {"id": "zenodo:7834392", "format": "ro-crate"})
    crate = out["ro_crate"]
    assert _flattening_violations(crate) == []

    graph = {e["@id"]: e for e in crate["@graph"]}
    authors = [ref["@id"] for ref in graph["./"]["author"]]
    assert len(authors) == len(out["creators"]) > 2
    assert graph["https://orcid.org/0000-0001-8499-824X"] == {
        "@id": "https://orcid.org/0000-0001-8499-824X",
        "@type": "Person",
        "name": "Banda, Juan M.",
    }
    local = [a for a in authors if a.startswith("#author-")]
    assert local, authors
    assert all(graph[a]["@type"] == "Person" and graph[a]["name"] for a in authors)
    assert len(graph["./"]["hasPart"]) == len(out["files"]) > 0

