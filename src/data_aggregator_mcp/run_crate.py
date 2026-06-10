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

SCOPE: intra-page only. The crate documents the search page just returned; a paginated
search yields one crate per page (stateless, mirroring B7). A cross-page crate is out of
scope (needs state the server lacks).

PURE: no network, no file I/O, deterministic. ``fair.assess`` (called per hit) is itself
pure, so ``render`` stays pure — no Crossref/trust per hit.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp import __version__, dossier, fair, ro_crate
from data_aggregator_mcp.models import DataResource, SearchResult


def _expansions(result: SearchResult) -> list[dict[str, Any]]:
    """One small object per ontology-expansion axis that ACTUALLY FIRED, naming the
    input, the ontology id, the canonical name, and the synonyms added. Axes that did
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
                "axis": label,
                "input": exp.input,
                "ontology_id": getattr(exp, id_field),
                "canonical_name": exp.canonical_name,
                "synonyms": list(exp.synonyms),
            }
        )
    return out


def _hit_identifier(hit: DataResource) -> str:
    """Best available stable identifier for a hit: DOI, else first file URL, else the
    canonical source-prefixed id (always present)."""
    if hit.doi:
        return f"https://doi.org/{hit.doi}"
    for f in hit.files:
        if f.url:
            return f.url
    return hit.id


def render(result: SearchResult) -> dict[str, Any]:
    """Render an RO-Crate 1.1 Run Crate for a whole search page. PURE, deterministic,
    no I/O. See the module docstring for the honesty contract and scope boundaries."""
    agent = {
        "@id": dossier.AGENT_ID,
        "@type": "SoftwareApplication",
        "name": "data-aggregator-mcp",
        "version": __version__,
    }

    hit_refs = [{"@id": f"#hit-{i}"} for i in range(len(result.results))]

    # The run-level provenance action. `sources_queried` is derived HONESTLY: it is the
    # union of the sources that returned at least one hit (read off each hit's `source`)
    # and the sources that reported an error (the `errors` keys). It is NOT the full
    # configured adapter set — a source that ran but returned zero hits and no error is
    # not recoverable from the SearchResult, so we do not claim it. The list means
    # "sources observed to have participated in this page", and no more.
    sources_queried = sorted({hit.source for hit in result.results} | set(result.errors.keys()))

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
    }
    expansions = _expansions(result)
    if expansions:
        action["ontology_expansions"] = expansions
    if result.errors:
        action["errors"] = dict(result.errors)

    root: dict[str, Any] = {
        "@id": "./",
        "@type": "Dataset",
        "name": f"Search run: {result.query}",
        "mentions": {"@id": "#search-action"},
        "hasPart": hit_refs,
    }

    descriptor = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": ro_crate.CONFORMS_TO},
        "about": {"@id": "./"},
    }

    graph: list[dict[str, Any]] = [descriptor, root, agent, action]

    for i, hit in enumerate(result.results):
        # Compute FAIR per hit (pure); NO per-hit trust/Crossref → retraction omitted.
        hit_with_fair = hit.model_copy(update={"fair": fair.assess(hit)})
        assessments = dossier.assessment_entities(hit_with_fair, id_prefix=f"hit-{i}-")
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

    return {"@context": ro_crate.CONTEXT, "@graph": graph}
