"""RO-Crate export — minimal RO-Crate 1.1 metadata for a resolved resource.

Renders the root data entity, its authors, its licence and its files as a flattened
RO-Crate @graph. Pure transform.
Complements the Croissant export: RO-Crate is the research-output packaging
standard (general datasets, software, papers), Croissant the ML-dataset one.

Also the home of the crates' shared vocabulary: the dossier and run crates carry
assessment fields RO-Crate's context does not define (``score``, ``is_latest``, ...). A
JSON-LD processor drops a key its context does not map, so each is declared in the
``@context`` (the array form RO-Crate 1.1 asks for) and defined in the graph as an
``rdf:Property``; ``docs/vocab.md`` documents them at the IRIs used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import Creator, DataResource

CONTEXT = "https://w3id.org/ro/crate/1.1/context"
CONFORMS_TO = "https://w3id.org/ro/crate/1.1"
ORCID_BASE = "https://orcid.org/"
SPDX_BASE = "https://spdx.org/licenses/"
VOCAB_BASE = "https://github.com/musharna/data-aggregator-mcp/blob/main/docs/vocab.md#"

# Every ad hoc term a crate may carry, with its definition. Keys a JSON-LD processor
# would otherwise drop; tests/test_crate_conformance.py expands each crate and fails on
# any key the context leaves unmapped, and on any term here docs/vocab.md lacks.
TERMS: dict[str, str] = {
    "is_latest": "Whether the record is the latest known version of its work.",
    "superseded_by": "Identifier of the newer version that supersedes the record.",
    "license_raw": "The licence exactly as the source states it.",
    "normalized_spdx": "The SPDX identifier the stated licence maps to, if any.",
    "score": "FAIR score of the record, 0 to 100.",
    "findable": "FAIR Findable sub-score, 0 to 100.",
    "accessible": "FAIR Accessible sub-score, 0 to 100.",
    "interoperable": "FAIR Interoperable sub-score, 0 to 100.",
    "reusable": "FAIR Reusable sub-score, 0 to 100.",
    "assessed": "Number of FAIR indicators evaluated.",
    "gaps": "FAIR indicators the record does not meet.",
    "retracted": "Whether a retraction is on record (Crossref); absent when unchecked.",
    "concern": "Whether an expression of concern is on record (Crossref).",
    "retraction_doi": "DOI of the retraction notice.",
    "source": "The repository the record was resolved from.",
    "canonical_id": "The record's source-prefixed identifier in this server.",
    "doi": "The record's DOI, without a resolver prefix.",
    "identifiers": "Other identifiers of the record, one PropertyValue each.",
    "accessions": "Database accessions the record cites.",
    "links": "Qualified relations of the record (relation name to target id).",
    "result_count": "Number of hits on this search page.",
    "total": "Total number of hits the sources report for the query.",
    "sources_queried": "Sources observed to take part in this search page.",
    "ontology_expansions": "Ontology terms the query was expanded with.",
    "axis": "The search filter an ontology expansion applies to.",
    "matched_input": "The text the ontology expansion was looked up from.",
}


def context() -> list[Any]:
    """The crate ``@context``: RO-Crate 1.1's, then a term for each ad hoc key."""
    return [CONTEXT, {key: VOCAB_BASE + key for key in TERMS}]


def term_definitions(graph: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An ``rdf:Property`` entity for each ad hoc term the graph uses, in TERMS order."""
    used = {key for ent in graph for key in ent}
    return [
        {
            "@id": VOCAB_BASE + key,
            "@type": "rdf:Property",
            "rdfs:label": key,
            "rdfs:comment": comment,
        }
        for key, comment in TERMS.items()
        if key in used
    ]


def timestamp(now: datetime) -> str:
    """``now`` as an ISO 8601 UTC timestamp to the second, as RO-Crate dates take it."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _person_id(c: Creator, i: int) -> str:
    """A creator's ``@id``: its ORCID iD, which RO-Crate 1.1 recommends for a Person,
    else a crate-local ``#author-<n>``. An id is not optional: RO-Crate 1.1 requires a
    flattened ``@graph``, where a nested object may hold nothing but ``@id``."""
    return f"{ORCID_BASE}{c.orcid}" if c.orcid else f"#author-{i}"


def _license_entity(r: DataResource) -> dict[str, Any]:
    """The root's licence as the contextual entity RO-Crate 1.1 asks it to link to. A
    licence that maps to SPDX is that licence's SPDX page; one that does not is the text
    as stated; a record with none says so, never naming a licence the source did not."""
    if not r.license:
        return {
            "@id": "#license",
            "@type": "CreativeWork",
            "name": "No licence stated",
            "description": f"{r.source} states no licence for this record; "
            "ask the source before reuse.",
        }
    spdx = normalize_spdx(r.license)
    if spdx is not None:
        return {"@id": SPDX_BASE + spdx, "@type": "CreativeWork", "name": spdx}
    return {
        "@id": "#license",
        "@type": "CreativeWork",
        "name": r.license,
        "description": f"Licence as {r.source} states it; not a recognised SPDX licence.",
    }


def render(r: DataResource, *, created: datetime) -> dict[str, Any]:
    """The crate for ``r``, made at ``created``. RO-Crate 1.1 requires the root's
    description, licence and datePublished: a record without a description gets one
    naming its source and id, and one without a year is dated by the crate."""
    description = r.description or f"{r.source} record {r.id}; the source gives no description."
    license_entity = _license_entity(r)
    root: dict[str, Any] = {
        "@id": "./",
        "@type": "Dataset",
        "name": r.title,
        "description": description,
        "datePublished": str(r.year) if r.year else created.astimezone(UTC).date().isoformat(),
        "license": {"@id": license_entity["@id"]},
    }
    if r.doi:
        root["identifier"] = f"https://doi.org/{r.doi}"
    # Each author is its own Person entity, referenced by @id; a creator listed twice
    # under one ORCID is one person, so one entity.
    persons: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(r.creators):
        pid = _person_id(c, i)
        persons.setdefault(pid, {"@id": pid, "@type": "Person", "name": c.name})
    if persons:
        root["author"] = [{"@id": pid} for pid in persons]

    file_entities: list[dict[str, Any]] = []
    has_part: list[dict[str, str]] = []
    for f in r.files:
        fid = f.url or f.name
        has_part.append({"@id": fid})
        ent: dict[str, Any] = {"@id": fid, "@type": "File", "name": f.name}
        if f.mime:
            ent["encodingFormat"] = f.mime
        if f.size is not None:
            ent["contentSize"] = f.size
        file_entities.append(ent)
    root["hasPart"] = has_part

    return {
        "@context": CONTEXT,
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "conformsTo": {"@id": CONFORMS_TO},
                "about": {"@id": "./"},
            },
            root,
            license_entity,
            *persons.values(),
            *file_entities,
        ],
    }
