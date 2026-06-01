"""Croissant export — file-level subset of the Croissant 1.1 metadata format.

This renders the dataset + its FileObjects (the file-level layer). It does NOT
emit RecordSet/Field structures: those describe tabular column semantics, which
require reading file internals (a later operate-on-data capability). The output
is therefore a valid schema.org Dataset with Croissant FileObject distributions,
not a RecordSet-complete 1.1 manifest. Pure transform — never does I/O.
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp.models import DataResource

_CONTEXT = {
    "@vocab": "https://schema.org/",
    "cr": "http://mlcommons.org/croissant/",
    "sc": "https://schema.org/",
}


def _file_object(
    name: str, url: str | None, mime: str | None, size: int | None, checksum: str | None
) -> dict[str, Any]:
    obj: dict[str, Any] = {"@type": "cr:FileObject", "@id": name, "name": name}
    if url:
        obj["contentUrl"] = url
    if mime:
        obj["encodingFormat"] = mime
    if size is not None:
        obj["contentSize"] = size
    if checksum and ":" in checksum:
        algo, _, hexval = checksum.partition(":")
        if algo in ("sha256", "md5"):
            obj[algo] = hexval
    return obj


def render(r: DataResource) -> dict[str, Any]:
    """Render ``r`` as a file-level Croissant JSON-LD object."""
    out: dict[str, Any] = {
        "@context": _CONTEXT,
        "@type": "Dataset",
        "name": r.title,
    }
    if r.description:
        out["description"] = r.description
    if r.doi:
        out["identifier"] = f"https://doi.org/{r.doi}"
    if r.license:
        out["license"] = r.license
    if r.year:
        out["datePublished"] = str(r.year)
    if r.creators:
        out["creator"] = [{"@type": "Person", "name": c.name} for c in r.creators]
    out["distribution"] = [_file_object(f.name, f.url, f.mime, f.size, f.checksum) for f in r.files]
    return out
