"""Contract for the ontology-expansion module extracted from the router (O4).

Expansion behavior itself is covered by tests/test_router.py and the per-registry test
modules, which reach it through router's historical names.
"""

from __future__ import annotations

import ast
from pathlib import Path

from data_aggregator_mcp import _ontology, router, tool_specs


def test_router_reexports_are_the_extracted_objects():
    assert router._or_group is _ontology.or_group
    assert router._unresolved_entities is _ontology.unresolved_entities
    assert router._ONTOLOGY_FIELDS is _ontology.ONTOLOGY_FIELDS
    assert router._expand_organism is _ontology.expand_organism
    assert router._expand_disease is _ontology.expand_disease
    assert router._expand_tissue is _ontology.expand_tissue
    assert router._expand_chemical is _ontology.expand_chemical
    assert router._expand_assay is _ontology.expand_assay


def test_every_advertised_ontology_filter_has_an_expander():
    """The search tool advertises exactly these entity filters. A param added to the
    schema without a matching expander would be accepted and then silently ignored —
    the same advertise/implement gap as the uniprot bug, one layer up."""
    declared = {field for field, _ontology_name, _err in _ontology.ONTOLOGY_FIELDS}
    search = next(t for t in tool_specs.TOOLS if t.name == "search")
    advertised = declared & set(search.inputSchema["properties"])
    assert advertised == declared, sorted(declared - advertised)
    for field in declared:
        assert callable(getattr(_ontology, f"expand_{field}")), field


def test_ontology_field_order_is_the_unresolved_echo_order():
    """SearchResult.unresolved order is a public payload detail fixed by this table."""
    assert [f for f, _, _ in _ontology.ONTOLOGY_FIELDS] == [
        "organism",
        "disease",
        "tissue",
        "chemical",
        "assay",
    ]


def test_ontology_does_not_import_the_router():
    tree = ast.parse(Path(_ontology.__file__ or "").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m.endswith("router")}, sorted(imported)
