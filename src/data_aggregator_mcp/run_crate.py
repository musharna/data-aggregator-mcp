"""Run Crate — an RO-Crate 1.1 provenance manifest for a WHOLE search.

``render(result)`` builds a single machine-readable artifact documenting an entire
search page: the RUN itself (the query, the sources queried, the ontology expansions
that fired, and the per-source errors) as a schema.org ``CreateAction``, PLUS per-hit
provenance for every result record — version-currency (B1), licence + normalized SPDX
(B3), and FAIRness (B4) — composed by REUSING B10a's ``dossier.assessment_entities``.
This is the "why an aggregator" artifact: one call yields a machine-readable provenance
manifest for an entire search.

HONESTY (inherited + extended from B10a):
- Per-hit RETRACTION is OMITTED — search hits carry ``trust=None``, so
  ``dossier._retraction_result`` returns None and emits no entity (an honest absence,
  never a negative claim). The run crate does NOT fan out N Crossref calls; per-hit
  retraction stays a per-record opt-in (``resolve(format=provenance)`` / B10a).
- Ontology expansions are echoed ONLY for the axes that actually fired (each
  ``*_expansion`` field that is not None) — a search with no expansions emits NO
  expansion block, never a fabricated one.
- Per-source ``errors`` are disclosed verbatim — a partial search is shown, not hidden.
- ``conformsTo`` stays ONLY on the metadata descriptor (RO-Crate 1.1) — no profile URI.
- The crate licenses nothing: its root ``license`` says each hit keeps its own licence
  (each hit's licence assessment names it), rather than naming one for the page.

CONFORMANCE: RO-Crate 1.1 as rocrate-validator reads it. The root carries the
description, licence and ``datePublished`` (the run time) the spec requires; the run's
own fields (``result_count``, ``sources_queried``, ...) are declared ad hoc terms
(``ro_crate.TERMS``) so a JSON-LD processor keeps them; and the graph is flat: each
ontology expansion is a ``DefinedTerm`` entity and each per-source error a
``PropertyValue`` entity under schema.org ``error``.

SCOPE: intra-page only. The crate documents the search page just returned; a paginated
search yields one crate per page (stateless, mirroring B7). A cross-page crate is out of
scope (needs state the server lacks).

PURE: no network, no file I/O, deterministic for a given ``now`` (the handler reads the
clock). ``fair.assess`` (called per hit) is itself pure, so ``render`` stays pure — no
Crossref/trust per hit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from data_aggregator_mcp import __version__, dossier, fair, ro_crate, sources
from data_aggregator_mcp.models import DataResource, SearchResult

_LICENSE_ID = "#license"


def _expansions(result: SearchResult) -> list[dict[str, Any]]:
    """One ``DefinedTerm`` entity per ontology-expansion axis that ACTUALLY FIRED, naming
    the input, the ontology id, the canonical name, and the synonyms added. Axes that did
    not fire (field is None) emit nothing — no fabricated expansion block."""
    out: list[dict[str, Any]] = []
    # (axis label, the *_expansion attribute, the id-field name on that model)
    axes = (
        ("taxon", result.taxon_expansion, "taxid"),
        ("mesh", result.mesh_expansion, "mesh_ui"),
        ("tissue", result.tissue_expansion, "uberon_id"),
        ("chemical", result.chemical_expansion, "chebi_id"),
        ("assay", result.assay_expansion, "edam_id"),
    )
    for label, exp, id_field in axes:
        if exp is None:
            continue
        out.append(
            {
                "@id": f"#expansion-{label}",
                "@type": "DefinedTerm",
                "name": exp.canonical_name,
                "termCode": getattr(exp, id_field),
                "alternateName": list(exp.synonyms),
                "axis": label,
                "matched_input": exp.input,
            }
        )
    return out


def _errors(result: SearchResult) -> list[dict[str, Any]]:
    """One ``PropertyValue`` entity per ``errors`` entry, in key order: the key (a source
    stream or a run note) as ``name``, the message as ``value``."""
    return [
        {"@id": f"#error-{n}", "@type": "PropertyValue", "name": key, "value": message}
        for n, (key, message) in enumerate(sorted(result.errors.items()))
    ]


def _failed_source(key: str) -> str | None:
    """The data source a ``SearchResult.errors`` key reports as failed, named as that
    source's records name it, or None when the key is not a source.

    The router keys a failed stream by the adapter (``zenodo``) or, for a composite
    adapter, ``<adapter>/<sub>`` (``omics/sra``, whose records say ``sra``), or, for the
    title half of an adapter with a ``TITLE_FIELD``, ``<adapter>/title``, with ``#v<n>``
    appended for a multi-query variant (``router._source_streams``). Every
    other key is a note about the run (``filters``, ``semantic``, ``query_syntax``,
    ``understand``, ``multi_query``) or a failed ontology lookup (``taxonomy``,
    ``mesh``, ...), and naming those as data sources would be a false claim."""
    stream, variant_mark, variant = key.partition("#v")
    if variant_mark and not variant.isdecimal():
        return None
    name, sub_mark, sub = stream.partition("/")
    adapter = sources.ADAPTERS.get(name)
    if adapter is None:
        return None
    if not sub_mark or (sub == "title" and getattr(adapter, "TITLE_FIELD", None)):
        return name
    return sub if sub in getattr(adapter, "SUBSOURCES", ()) else None


def _hit_identifier(hit: DataResource) -> str:
    """Best available stable identifier for a hit: DOI, else first file URL, else the
    canonical source-prefixed id (always present)."""
    if hit.doi:
        return f"https://doi.org/{hit.doi}"
    for f in hit.files:
        if f.url:
            return f.url
    return hit.id


def render(result: SearchResult, *, now: datetime) -> dict[str, Any]:
    """Render an RO-Crate 1.1 Run Crate for a whole search page run at ``now``. PURE,
    deterministic for a given ``now``, no I/O. See the module docstring for the honesty
    contract and scope boundaries."""
    ran = ro_crate.timestamp(now)
    agent = {
        "@id": dossier.AGENT_ID,
        "@type": "SoftwareApplication",
        "name": "data-aggregator-mcp",
        "version": __version__,
    }

    hit_refs = [{"@id": f"#hit-{i}"} for i in range(len(result.results))]

    # The run-level provenance action. `sources_queried` is derived HONESTLY: it is the
    # union of the sources that returned at least one hit (read off each hit's `source`)
    # and the sources whose search failed (the `errors` keys that name a source stream;
    # see `_failed_source` -- the run notes in `errors` are not sources). It is NOT the full
    # configured adapter set — a source that ran but returned zero hits and no error is
    # not recoverable from the SearchResult, so we do not claim it. The list means
    # "sources observed to have participated in this page", and no more.
    failed = {src for key in result.errors if (src := _failed_source(key)) is not None}
    sources_queried = sorted({hit.source for hit in result.results} | failed)

    action: dict[str, Any] = {
        "@id": "#search-action",
        "@type": "CreateAction",
        "name": "data-aggregator-mcp search",
        "instrument": {"@id": dossier.AGENT_ID},
        "object": {"@id": "./"},
        "query": result.query,
        "result_count": result.count,
        "total": result.total,
        "sources_queried": sources_queried,
        "result": hit_refs,
        "endTime": ran,
    }
    expansions = _expansions(result)
    if expansions:
        action["ontology_expansions"] = [{"@id": ent["@id"]} for ent in expansions]
    errors = _errors(result)
    if errors:
        action["error"] = [{"@id": ent["@id"]} for ent in errors]

    root: dict[str, Any] = {
        "@id": "./",
        "@type": "Dataset",
        "name": f"Search run: {result.query}",
        "description": (
            f"Provenance of a data-aggregator-mcp search for {result.query!r}: "
            f"{result.count} hits on this page, {result.total} reported in all."
        ),
        "datePublished": ran,
        "license": {"@id": _LICENSE_ID},
        "mentions": {"@id": "#search-action"},
        "hasPart": hit_refs,
    }
    license_entity = {
        "@id": _LICENSE_ID,
        "@type": "CreativeWork",
        "name": "Each hit keeps its own licence",
        "description": "This crate describes records it does not license; each hit's "
        "licence assessment names the licence its source states.",
    }

    descriptor = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": ro_crate.CONFORMS_TO},
        "about": {"@id": "./"},
    }

    graph: list[dict[str, Any]] = [descriptor, root, license_entity, agent, action]
    graph.extend(expansions)
    graph.extend(errors)

    for i, hit in enumerate(result.results):
        # Compute FAIR per hit (pure); NO per-hit trust/Crossref → retraction omitted.
        hit_with_fair = hit.model_copy(update={"fair": fair.assess(hit)})
        assessments, pieces = dossier.assessment_entities(hit_with_fair, id_prefix=f"hit-{i}-")
        hit_entity: dict[str, Any] = {
            "@id": f"#hit-{i}",
            "@type": "Dataset",
            "name": hit.title,
            "identifier": _hit_identifier(hit),
            "mentions": [{"@id": ent["@id"]} for ent in assessments],
        }
        if hit.license:
            hit_entity["license"] = hit.license
        graph.append(hit_entity)
        graph.extend(assessments)
        graph.extend(pieces)

    graph.extend(ro_crate.term_definitions(graph))
    return {"@context": ro_crate.context(), "@graph": graph}
