"""Croissant export — file-level subset of the Croissant 1.1 metadata format.

This renders the dataset + its FileObjects (the file-level layer) as a
Croissant **1.1**-conformant manifest (it carries the ``conformsTo`` version
marker and dataset-level PROV-O provenance). It does NOT emit RecordSet/Field
structures: those describe tabular column semantics, which require reading file
internals (a later operate-on-data capability). The output is therefore a valid
schema.org Dataset with Croissant FileObject distributions plus dataset-level
provenance, not a RecordSet-complete 1.1 manifest. Pure transform — never does
I/O (in particular it never calls the async ``citation.render``; it only reuses
an already-populated ``r.citation``).
"""

from __future__ import annotations

from typing import Any

from data_aggregator_mcp.models import Creator, DataResource, Link

_CONTEXT = {
    "@vocab": "https://schema.org/",
    "cr": "http://mlcommons.org/croissant/",
    "sc": "https://schema.org/",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    # odrl prefix reserved for B3's license-compatibility policy (odrl:Offer).
    # B2 emits no odrl: keys; the prefix is declared now so B3 needs no context change.
    "odrl": "http://www.w3.org/ns/odrl/2/",
}

_CONFORMS_TO = "http://mlcommons.org/croissant/1.1"

# Conservative derivation rels: "this entity came FROM that target". Mapping any
# other rel (is_supplement_to/cites/part_of/...) to prov:wasDerivedFrom would
# overstate provenance — the cardinal honesty failure for a provenance feature.
_DERIVATION_RELS = {"is_derived_from", "is_version_of", "is_new_version_of"}

# source string -> human-readable publisher name; raw source used as fallback.
_SOURCE_DISPLAY = {
    "zenodo": "Zenodo",
    "datacite": "DataCite",
    "dryad": "Dryad",
    "geo": "NCBI GEO",
    "sra": "NCBI SRA",
    "pubmed": "PubMed",
    "europepmc": "Europe PMC",
    "dataone": "DataONE",
    "huggingface": "Hugging Face",
    "openalex": "OpenAlex",
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


def _agent(creator: Creator) -> dict[str, Any]:
    """A PROV agent (Person) for ``prov:wasAttributedTo``. Adds an ``@id`` ORCID
    URL only when the creator carries an ORCID (already-validated bare form)."""
    person: dict[str, Any] = {"@type": "Person"}
    if creator.orcid:
        person["@id"] = f"https://orcid.org/{creator.orcid}"
    person["name"] = creator.name
    return person


def _target_id(target_id: str) -> str:
    """Resolve a link target to an @id. A URL passes through; a DOI (``10.…``)
    gets the doi.org prefix; ANY OTHER identifier (a bare accession like
    ``GSE12345``, a PMID, …) is emitted verbatim — fabricating a ``doi.org`` URL
    for a non-DOI would assert a false DOI, the cardinal provenance-honesty
    failure for a provenance feature."""
    if target_id.startswith(("http://", "https://")):
        return target_id
    if target_id.startswith("10."):  # DOI
        return f"https://doi.org/{target_id}"
    return target_id


def _derived_from(links: list[Link]) -> list[dict[str, str]]:
    """``prov:wasDerivedFrom`` entries from the conservative derivation rels
    only (see ``_DERIVATION_RELS``). Returns [] when no derivation link exists."""
    return [{"@id": _target_id(lnk.target_id)} for lnk in links if lnk.rel in _DERIVATION_RELS]


def render(r: DataResource) -> dict[str, Any]:
    """Render ``r`` as a file-level Croissant 1.1 JSON-LD object."""
    out: dict[str, Any] = {
        "@context": _CONTEXT,
        "@type": "Dataset",
        "conformsTo": _CONFORMS_TO,
        "name": r.title,
    }
    if r.description:
        out["description"] = r.description
    if r.doi:
        out["identifier"] = f"https://doi.org/{r.doi}"
    if r.subjects:
        out["keywords"] = list(r.subjects)
    if r.license:
        out["license"] = r.license
        # B2: a license POINTER only — no fabricated odrl:Offer/permissions.
        # B3 (license-compatibility) upgrades this to a full odrl:Offer policy.
        out["usageInfo"] = r.license
    if r.year:
        out["datePublished"] = str(r.year)
    if r.last_updated:
        out["dateModified"] = r.last_updated
    named = [c for c in r.creators if c.name and c.name.strip()]
    if named:
        out["creator"] = [{"@type": "Person", "name": c.name} for c in named]
        out["prov:wasAttributedTo"] = [_agent(c) for c in named]
    derived = _derived_from(r.links)
    if derived:
        out["prov:wasDerivedFrom"] = derived
    out["publisher"] = {
        "@type": "Organization",
        "name": _SOURCE_DISPLAY.get(r.source, r.source),
    }
    # citeAs reuses an already-populated bibtex citation only (pure transform —
    # never renders one here); a CSL-text citation is not a citeAs and is omitted.
    if r.citation and r.citation.startswith("@"):
        out["citeAs"] = r.citation
    out["distribution"] = [_file_object(f.name, f.url, f.mime, f.size, f.checksum) for f in r.files]
    return out
