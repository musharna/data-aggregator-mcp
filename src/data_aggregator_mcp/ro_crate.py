"""RO-Crate export — minimal RO-Crate 1.1 metadata for a resolved resource.

Renders the root data entity + its files as an RO-Crate @graph. Pure transform.
Complements the Croissant export: RO-Crate is the research-output packaging
standard (general datasets, software, papers), Croissant the ML-dataset one.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp.models import DataResource

CONTEXT = "https://w3id.org/ro/crate/1.1/context"
CONFORMS_TO = "https://w3id.org/ro/crate/1.1"


def render(r: DataResource) -> dict[str, Any]:
    root: dict[str, Any] = {"@id": "./", "@type": "Dataset", "name": r.title}
    if r.description:
        root["description"] = r.description
    if r.doi:
        root["identifier"] = f"https://doi.org/{r.doi}"
    if r.license:
        root["license"] = r.license
    if r.year:
        root["datePublished"] = str(r.year)
    if r.creators:
        root["author"] = [{"@type": "Person", "name": c.name} for c in r.creators]

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
            *file_entities,
        ],
    }
