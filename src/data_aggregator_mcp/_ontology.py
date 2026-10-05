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

import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from data_aggregator_mcp import anatomy, chemistry, mesh, taxonomy
from data_aggregator_mcp import assay as assay_mod
from data_aggregator_mcp.models import (
    AssayExpansion,
    ChemicalExpansion,
    MeshExpansion,
    TaxonAlternative,
    TaxonExpansion,
    TermAlternative,
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


@dataclass(frozen=True)
class FacetGroup:
    """One facet as an expander ANDs it onto the query: the name the caller typed and
    the terms OR'd for it, in the order the expander emits them. Recorded so a source
    whose search caps its boolean operators can be sent a shorter rendering of the same
    expansion (see :func:`within_operator_limit`)."""

    input: str
    terms: tuple[str, ...]


def and_group(query: str, terms: Sequence[str]) -> str:
    """``query`` ANDed with one facet's OR group: the one rendering every expander and
    :func:`within_operator_limit` share, so a re-rendering that keeps every term is
    byte-identical to the expanded query."""
    return f"({query}) AND ({or_group(list(terms))})"


# A double-quoted phrase, or an operator word. Quoted phrases are matched first so the
# operator words inside them are skipped, as the upstream does.
_OPERATOR_OR_PHRASE = re.compile(r'"[^"]*"|\b(AND|OR|NOT)\b')


def count_operators(query: str) -> int:
    """The upper-case AND / OR / NOT words outside double quotes: what OpenAIRE's search
    counts against its limit. Probed live 2026-10-04: quoted operators, lower-case
    ``and``/``or`` and words such as ``ANDROGEN`` are not counted."""
    return sum(1 for word in _OPERATOR_OR_PHRASE.findall(query) if word)


def _typed_name_first(group: FacetGroup) -> list[str]:
    """The group's terms with the name the caller typed moved to the front, so a
    shortened expansion never drops the caller's own word."""
    typed = group.input.strip().casefold()
    first = [t for t in group.terms if t.strip().casefold() == typed]
    return first + [t for t in group.terms if t.strip().casefold() != typed]


def within_operator_limit(
    plain: str, groups: Sequence[FacetGroup], limit: int
) -> tuple[str, list[str]] | None:
    """Render ``plain`` ANDed with every facet in ``groups`` using at most ``limit``
    operators, for an upstream that rejects longer queries.

    The full expansion is returned unchanged when it fits. Otherwise every facet keeps
    the name the caller typed first, then its other terms in order, and the spare OR
    slots go one per facet in turn, so each facet keeps a second name before any keeps
    a third. Returns the query and the terms left out, or None when even one name per
    facet does not fit (every facet costs one AND)."""
    full = plain
    for group in groups:
        full = and_group(full, group.terms)
    if count_operators(full) <= limit:
        return full, []
    spare = limit - count_operators(plain) - len(groups)
    if spare < 0:
        return None
    ordered = [_typed_name_first(g) for g in groups]
    kept = [1] * len(ordered)
    while spare and any(k < len(o) for k, o in zip(kept, ordered, strict=True)):
        for i, terms in enumerate(ordered):
            if spare and kept[i] < len(terms):
                kept[i] += 1
                spare -= 1
    query, left_out = plain, []
    for terms, k in zip(ordered, kept, strict=True):
        query = and_group(query, terms[:k])
        left_out += terms[k:]
    return query, left_out


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
    client: httpx.AsyncClient,
    query: str,
    organism: str | None,
    errors: dict[str, str],
    groups: list[FacetGroup] | None = None,
) -> tuple[str, TaxonExpansion | None]:
    """If ``organism`` resolves, AND ``query`` with a (canonical OR synonyms)
    group and return the echo. A taxonomy lookup failure is recorded in
    ``errors['taxonomy']`` and the query is returned un-expanded (fail-loud:
    the caller sees expansion did not happen, never a silent 'no synonyms').
    Each expander appends the facet it ANDed to ``groups`` when one is passed.
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
    effective = and_group(query, terms)
    if groups is not None:
        groups.append(FacetGroup(input=organism, terms=tuple(terms)))
    expansion = TaxonExpansion(
        input=organism,
        taxid=info.taxid,
        canonical_name=info.canonical_name,
        synonyms=list(info.synonyms),
        alternatives=[TaxonAlternative(taxid=a.taxid, name=a.name) for a in info.alternatives],
    )
    return effective, expansion


async def expand_disease(
    client: httpx.AsyncClient,
    query: str,
    disease: str | None,
    errors: dict[str, str],
    groups: list[FacetGroup] | None = None,
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
    effective = and_group(query, terms)
    if groups is not None:
        groups.append(FacetGroup(input=disease, terms=tuple(terms)))
    expansion = MeshExpansion(
        input=disease,
        mesh_ui=info.ui,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def expand_tissue(
    client: httpx.AsyncClient,
    query: str,
    tissue: str | None,
    errors: dict[str, str],
    groups: list[FacetGroup] | None = None,
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
    effective = and_group(query, terms)
    if groups is not None:
        groups.append(FacetGroup(input=tissue, terms=tuple(terms)))
    expansion = TissueExpansion(
        input=tissue,
        uberon_id=info.uberon_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
        alternatives=[TermAlternative(id=a.id, label=a.label) for a in info.alternatives],
    )
    return effective, expansion


async def expand_chemical(
    client: httpx.AsyncClient,
    query: str,
    chemical: str | None,
    errors: dict[str, str],
    groups: list[FacetGroup] | None = None,
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
    effective = and_group(query, terms)
    if groups is not None:
        groups.append(FacetGroup(input=chemical, terms=tuple(terms)))
    expansion = ChemicalExpansion(
        input=chemical,
        chebi_id=info.chebi_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
        alternatives=[TermAlternative(id=a.id, label=a.label) for a in info.alternatives],
    )
    return effective, expansion


async def expand_assay(
    client: httpx.AsyncClient,
    query: str,
    assay: str | None,
    errors: dict[str, str],
    groups: list[FacetGroup] | None = None,
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
    effective = and_group(query, terms)
    if groups is not None:
        groups.append(FacetGroup(input=assay, terms=tuple(terms)))
    expansion = AssayExpansion(
        input=assay,
        edam_id=info.edam_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion
