"""Pin each ``_ontology.expand_*``: the lookup it makes, the query it builds, the echo it
returns, and the ``errors`` key and text a failed lookup leaves."""

from __future__ import annotations

from typing import Any

import pytest

from data_aggregator_mcp import _ontology, anatomy, assay, chemistry, mesh, taxonomy
from data_aggregator_mcp.models import UnresolvedEntity

_TAXON = taxonomy.TaxonInfo(
    taxid=4081, canonical_name="Solanum lycopersicum", synonyms=("tomato", "tomato"), is_plant=True
)
# (expander, module, resolver name, info, errors key, echo id field, echo id)
_CASES: list[tuple[str, Any, str, Any, str, str, Any]] = [
    ("expand_organism", taxonomy, "resolve_taxon", _TAXON, "taxonomy", "taxid", 4081),
    (
        "expand_disease",
        mesh,
        "resolve_mesh",
        mesh.MeshInfo(ui="D001249", canonical="Asthma", synonyms=('Asthma "Bronchial"', "Asthma")),
        "mesh",
        "mesh_ui",
        "D001249",
    ),
    (
        "expand_tissue",
        anatomy,
        "resolve_uberon",
        anatomy.UberonInfo(uberon_id="UBERON:0002107", canonical="liver", synonyms=("hepar",)),
        "uberon",
        "uberon_id",
        "UBERON:0002107",
    ),
    (
        "expand_chemical",
        chemistry,
        "resolve_chebi",
        chemistry.ChebiInfo(chebi_id="CHEBI:27732", canonical="caffeine", synonyms=("guaranine",)),
        "chebi",
        "chebi_id",
        "CHEBI:27732",
    ),
    (
        "expand_assay",
        assay,
        "resolve_edam",
        assay.EdamInfo(
            edam_id="EDAM:topic_3169", canonical="ChIP-seq", synonyms=("ChIP-sequencing",)
        ),
        "edam",
        "edam_id",
        "EDAM:topic_3169",
    ),
]
_IDS = [c[0] for c in _CASES]
# The group each case must produce, written out: duplicates dropped, quotes neutralized.
_GROUPS = {
    "expand_organism": '"Solanum lycopersicum" OR "tomato"',
    "expand_disease": '"Asthma" OR "Asthma  Bronchial"',
    "expand_tissue": '"liver" OR "hepar"',
    "expand_chemical": '"caffeine" OR "guaranine"',
    "expand_assay": '"ChIP-seq" OR "ChIP-sequencing"',
}


def _canonical(info: Any) -> str:
    return getattr(info, "canonical", None) or info.canonical_name


@pytest.mark.parametrize(
    ("expander", "mod", "resolver", "info", "key", "id_field", "id_value"), _CASES, ids=_IDS
)
async def test_a_resolved_entity_ands_its_names_onto_the_query(
    monkeypatch, expander, mod, resolver, info, key, id_field, id_value
) -> None:
    seen: list[tuple[object, str]] = []

    async def fake(client, name):
        seen.append((client, name))
        return info

    monkeypatch.setattr(mod, resolver, fake)
    errors: dict[str, str] = {}
    query, echo = await getattr(_ontology, expander)("CLIENT", "rna seq", " Given Name ", errors)
    assert seen == [("CLIENT", " Given Name ")]  # passed through as given
    assert query == f"(rna seq) AND ({_GROUPS[expander]})"
    assert echo is not None and errors == {}
    assert echo.input == " Given Name "
    assert getattr(echo, id_field) == id_value
    assert echo.canonical_name == _canonical(info)
    assert echo.synonyms == list(info.synonyms)


def test_or_group_neutralizes_quotes_and_drops_empty_terms_but_keeps_repeats() -> None:
    assert _ontology.or_group(["Asthma", 'Asthma "Bronchial"', "Asthma"]) == (
        '"Asthma" OR "Asthma  Bronchial" OR "Asthma"'
    )
    assert _ontology.or_group(['"', "  ", "a"]) == '"a"'


@pytest.mark.parametrize(
    ("expander", "mod", "resolver", "info", "key", "id_field", "id_value"), _CASES, ids=_IDS
)
async def test_a_failed_lookup_is_recorded_under_its_registry_key(
    monkeypatch, expander, mod, resolver, info, key, id_field, id_value
) -> None:
    async def boom(client, name):
        raise TimeoutError("registry slow")

    monkeypatch.setattr(mod, resolver, boom)
    errors: dict[str, str] = {"other": "kept"}
    result = await getattr(_ontology, expander)(None, "q", "x", errors)
    assert result == ("q", None)
    assert errors == {"other": "kept", key: "TimeoutError: registry slow"}


@pytest.mark.parametrize(
    ("expander", "mod", "resolver", "info", "key", "id_field", "id_value"), _CASES, ids=_IDS
)
async def test_no_match_and_blank_input_leave_the_query_alone(
    monkeypatch, expander, mod, resolver, info, key, id_field, id_value
) -> None:
    seen: list[str] = []

    async def none(client, name):
        seen.append(name)
        return None

    monkeypatch.setattr(mod, resolver, none)
    errors: dict[str, str] = {}
    expand = getattr(_ontology, expander)
    for blank in (None, "", "   "):
        assert await expand(None, "q", blank, errors) == ("q", None)
    assert seen == []  # a blank entity makes no lookup
    assert await expand(None, "q", "nothing", errors) == ("q", None)
    assert seen == ["nothing"] and errors == {}


def test_unresolved_lists_each_supplied_unmatched_entity_in_table_order() -> None:
    supplied = {
        "assay": "ChIP",
        "organism": "yeast",
        "disease": "  ",
        "tissue": "root",
        "chemical": "caffeine",
    }
    out = _ontology.unresolved_entities(
        supplied, {"chemical": object(), "assay": None}, {"uberon": "boom"}
    )
    assert out == [
        UnresolvedEntity(
            field="organism",
            input="yeast",
            ontology="NCBI Taxonomy",
            note="NCBI Taxonomy returned no match for organism='yeast'; "
            "the search ran WITHOUT that expansion",
        ),
        UnresolvedEntity(
            field="assay",
            input="ChIP",
            ontology="EDAM",
            note="EDAM returned no match for assay='ChIP'; the search ran WITHOUT that expansion",
        ),
    ]
    assert _ontology.unresolved_entities({}, {}, {}) == []
    assert _ontology.unresolved_entities({"organism": None}, {}, {}) == []
