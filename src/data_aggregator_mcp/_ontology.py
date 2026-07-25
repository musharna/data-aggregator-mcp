"""Ontology-driven query expansion for the search path.

Each ``expand_*`` turns a caller-supplied entity name into a synonym-expanded query
plus an echo describing what happened, and :func:`unresolved_entities` derives the
"you asked for X but nothing matched" report from that same state.

Two invariants hold across all five, and they are the reason these live together:

* **A lookup FAILURE is recorded in ``errors`` and the query is returned un-expanded.**
  These are search-INPUT expansions, not fail-soft resolve enrichers — the caller must
  be able to see that expansion did not happen rather than infer "no synonyms exist".
* **A no-MATCH is not an error.** Nothing is recorded; the miss surfaces through
  :func:`unresolved_entities` instead, which keeps the two channels disjoint.

Extracted from ``router``, which now just sequences these calls. No router import
here — the dependency runs one way.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import anatomy, chemistry, mesh, taxonomy
from data_aggregator_mcp import assay as assay_mod
from data_aggregator_mcp.models import (
    AssayExpansion,
    ChemicalExpansion,
    MeshExpansion,
    TaxonExpansion,
    TissueExpansion,
    UnresolvedEntity,
)


def or_group(terms: list[str]) -> str:
    """Build a quoted ``"a" OR "b"`` group for query expansion, neutralizing any
    embedded double-quote in a term. Free-text ontology labels (NCBI Taxonomy
    synonyms, MeSH entry terms) must not break the surrounding quoting handed to
    downstream adapters. Terms that are empty after neutralization are dropped.
    Shared by every expander so the safety lives in one place."""
    safe = [t.replace('"', " ").strip() for t in terms]
    return " OR ".join(f'"{t}"' for t in safe if t)


# (param name, registry label, the errors[] key that module writes on a LOOKUP FAILURE).
# Order fixes the emitted order of SearchResult.unresolved.
ONTOLOGY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("organism", "NCBI Taxonomy", "taxonomy"),
    ("disease", "MeSH", "mesh"),
    ("tissue", "UBERON", "uberon"),
    ("chemical", "ChEBI", "chebi"),
    ("assay", "EDAM", "edam"),
)


def unresolved_entities(
    supplied: dict[str, str | None],
    expansions: dict[str, object | None],
    errors: dict[str, str],
) -> list[UnresolvedEntity]:
    """Pure: derive the no-match echo from state the search path already has.

    An entity is *unresolved* when the caller supplied it, the corresponding
    ``*_expansion`` echo is None, and no lookup FAILURE was recorded for that
    registry. That last clause keeps the two channels disjoint: a lookup that blew
    up is already reported in ``errors`` (fail-loud), and reporting it here as well
    would claim the registry answered "no match" when it never answered at all.

    Derived rather than threaded through the five ``expand_*`` signatures so the
    multi-query variant loop — which deliberately discards its per-variant echoes —
    cannot double-count.
    """
    out: list[UnresolvedEntity] = []
    for field, ontology, error_key in ONTOLOGY_FIELDS:
        value = supplied.get(field)
        if not value or not value.strip():
            continue
        if expansions.get(field) is not None:
            continue
        if error_key in errors:
            continue
        out.append(
            UnresolvedEntity(
                field=field,
                input=value,
                ontology=ontology,
                note=(
                    f"{ontology} returned no match for {field}={value!r}; "
                    "the search ran WITHOUT that expansion"
                ),
            )
        )
    return out


async def expand_organism(
    client: httpx.AsyncClient, query: str, organism: str | None, errors: dict[str, str]
) -> tuple[str, TaxonExpansion | None]:
    """If ``organism`` resolves, AND ``query`` with a (canonical OR synonyms)
    group and return the echo. A taxonomy lookup failure is recorded in
    ``errors['taxonomy']`` and the query is returned un-expanded (fail-loud:
    the caller sees expansion did not happen, never a silent 'no synonyms').
    """
    if not organism or not organism.strip():
        return query, None
    try:
        info = await taxonomy.resolve_taxon(client, organism)
    except Exception as exc:  # surfaced, not swallowed
        errors["taxonomy"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical_name, *info.synonyms]))
    group = or_group(terms)
    effective = f"({query}) AND ({group})"
    expansion = TaxonExpansion(
        input=organism,
        taxid=info.taxid,
        canonical_name=info.canonical_name,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def expand_disease(
    client: httpx.AsyncClient, query: str, disease: str | None, errors: dict[str, str]
) -> tuple[str, MeshExpansion | None]:
    """If ``disease`` resolves to a MeSH descriptor, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A MeSH lookup failure is
    recorded in ``errors['mesh']`` and the query is returned un-expanded
    (fail-loud — exactly like ``expand_organism``; this is a search-input
    expansion, NOT a fail-soft resolve enricher).
    """
    if not disease or not disease.strip():
        return query, None
    try:
        info = await mesh.resolve_mesh(client, disease)
    except Exception as exc:  # surfaced, not swallowed
        errors["mesh"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    group = or_group(terms)
    effective = f"({query}) AND ({group})"
    expansion = MeshExpansion(
        input=disease,
        mesh_ui=info.ui,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def expand_tissue(
    client: httpx.AsyncClient, query: str, tissue: str | None, errors: dict[str, str]
) -> tuple[str, TissueExpansion | None]:
    """If ``tissue`` resolves to a UBERON term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A UBERON (EBI OLS) lookup
    failure is recorded in ``errors['uberon']`` and the query is returned
    un-expanded (fail-loud — exactly like ``expand_organism``/``expand_disease``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not tissue or not tissue.strip():
        return query, None
    try:
        info = await anatomy.resolve_uberon(client, tissue)
    except Exception as exc:  # surfaced, not swallowed
        errors["uberon"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    group = or_group(terms)
    effective = f"({query}) AND ({group})"
    expansion = TissueExpansion(
        input=tissue,
        uberon_id=info.uberon_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def expand_chemical(
    client: httpx.AsyncClient, query: str, chemical: str | None, errors: dict[str, str]
) -> tuple[str, ChemicalExpansion | None]:
    """If ``chemical`` resolves to a ChEBI term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A ChEBI (EBI OLS) lookup
    failure is recorded in ``errors['chebi']`` and the query is returned
    un-expanded (fail-loud — exactly like ``expand_organism``/``expand_tissue``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not chemical or not chemical.strip():
        return query, None
    try:
        info = await chemistry.resolve_chebi(client, chemical)
    except Exception as exc:  # surfaced, not swallowed
        errors["chebi"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    group = or_group(terms)
    effective = f"({query}) AND ({group})"
    expansion = ChemicalExpansion(
        input=chemical,
        chebi_id=info.chebi_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def expand_assay(
    client: httpx.AsyncClient, query: str, assay: str | None, errors: dict[str, str]
) -> tuple[str, AssayExpansion | None]:
    """If ``assay`` resolves to an EDAM-topic term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. An EDAM (EBI OLS) lookup
    failure is recorded in ``errors['edam']`` and the query is returned
    un-expanded (fail-loud — exactly like ``expand_organism``/``expand_tissue``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not assay or not assay.strip():
        return query, None
    try:
        info = await assay_mod.resolve_edam(client, assay)
    except Exception as exc:  # surfaced, not swallowed
        errors["edam"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    group = or_group(terms)
    effective = f"({query}) AND ({group})"
    expansion = AssayExpansion(
        input=assay,
        edam_id=info.edam_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion
