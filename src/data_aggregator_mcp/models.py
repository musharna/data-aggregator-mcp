"""The wire contract — every source normalizes into DataResource."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _orcid(value: str | None) -> str | None:
    """Normalize an ORCID to its bare, canonical iD form, or None if
    absent/malformed. Uppercases the checksum char so a lowercase ``x`` is
    accepted and returned canonically.
    """
    if not value:
        return None
    bare = value.rsplit("/", 1)[-1].strip().upper()
    return bare if _ORCID_RE.match(bare) else None


def _rel(s: str) -> str:
    """Snake-case a DataCite/Zenodo relation type, e.g. IsSupplementTo →
    is_supplement_to, isPartOf → is_part_of."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


class Creator(BaseModel):
    name: str
    orcid: str | None = None


class FundingRef(BaseModel):
    funder: str
    award: str | None = None


class FileEntry(BaseModel):
    name: str
    size: int | None = None
    mime: str | None = None
    url: str | None = None
    checksum: str | None = None  # "<algo>:<hex>", e.g. "md5:abc123"
    source: str | None = None  # provenance label, e.g. "europepmc" | "unpaywall"


class Link(BaseModel):
    rel: str  # e.g. is_supplement_to, part_of, described_in
    target_id: str


class Mirror(BaseModel):
    """A same-dataset copy folded into this record by content dedup (resolve the
    mirror's id to reach the original deposit). Only populated when a search ran
    with the opt-in ``collapse_mirrors`` flag."""

    source: str
    id: str
    doi: str | None = None


class Taxon(BaseModel):
    taxid: int
    name: str  # canonical NCBI ScientificName


class Metrics(BaseModel):
    """Usage/impact signals, each a separate axis — NO blended score. All
    nullable: a source that does not expose an axis leaves it None."""

    citations: int | None = None
    views: int | None = None
    downloads: int | None = None
    likes: int | None = None


class TrustSignals(BaseModel):
    """Integrity/provenance signals attached on resolve(trust=True). All nullable:
    None = not checked or not determinable (e.g. a DOI Crossref doesn't register) —
    NEVER a negative claim. A *found* Crossref work yields definitive booleans."""

    retracted: bool | None = (
        None  # True = a Crossref retraction is on record; None = unchecked / not a Crossref work
    )
    retraction_doi: str | None = None  # DOI of the retraction notice, when retracted
    concern: bool | None = (
        None  # True = an expression-of-concern is on record (weaker integrity flag)
    )


class FairAssessment(BaseModel):
    """FAIRness assessment attached on resolve(fair=True). PURE-function output:
    a 0–100 overall score plus 0–100 per-dimension sub-scores, grounded in the
    machine-evaluable subset of the RDA FAIR Data Maturity Model. ``assessed`` is
    the count of indicators actually evaluated (transparency — we never score what
    the metadata can't show). ``gaps`` are failed-indicator reasons, each naming its
    RDA indicator id and framed as a metadata-exposure gap, not a value judgement."""

    score: int  # 0–100 overall = round(mean of the 4 dimension scores)
    findable: int  # 0–100
    accessible: int  # 0–100
    interoperable: int  # 0–100
    reusable: int  # 0–100
    assessed: int  # number of indicators evaluated
    gaps: list[str] = Field(default_factory=list)


class LicenseVerdict(BaseModel):
    """Licence-compatibility advisory attached on resolve(use=<intent>). PURE-function
    output: an ALLOW / REVIEW / DENY verdict for an intended use of the resolved record,
    computed from a bundled licence matrix (choosealicense.com flag vocabulary) keyed on
    the normalized SPDX id. ``spdx_id`` is None exactly when the licence was unrecognized
    or absent (→ REVIEW, never a fabricated ALLOW/DENY). ``reason`` names the governing
    clause; ``disclaimer`` states this is a metadata-derived advisory, not legal advice."""

    use: str  # the intended-use intent checked (commercial/redistribute/modify/ml-training)
    verdict: Literal["ALLOW", "REVIEW", "DENY"]
    spdx_id: str | None  # canonical SPDX id of the matched licence; None if unrecognized/absent
    license_raw: str | None  # the input licence string, verbatim
    reason: str  # human-readable, names the governing clause / why REVIEW
    disclaimer: str  # constant not-legal-advice advisory


class DataResource(BaseModel):
    id: str  # source-prefixed canonical id, e.g. "zenodo:123"
    source: str
    kind: str  # dataset | sequencing_run | study | publication | software
    title: str
    creators: list[Creator] = Field(default_factory=list)
    funding: list[FundingRef] = Field(default_factory=list)
    year: int | None = None
    description: str | None = None
    doi: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)  # cross-ids: pmid/pmcid/doi
    accessions: list[str] = Field(default_factory=list)
    organism: list[str] = Field(default_factory=list)
    taxa: list[Taxon] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    license: str | None = None
    access: str | None = None  # open | embargoed | restricted | closed | unknown
    files: list[FileEntry] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    citation: str | None = None  # rendered on resolve when cite= is requested
    metrics: Metrics | None = None  # usage/impact signals, source-dependent
    trust: TrustSignals | None = None  # integrity signals (retraction), on resolve(trust=True)
    fair: FairAssessment | None = None  # RDA-grounded FAIRness score, on resolve(fair=True)
    license_compat: LicenseVerdict | None = (
        None  # licence-compatibility preflight, on resolve(use=<intent>)
    )
    is_latest: bool | None = None  # None = no version info in links[]
    superseded_by: str | None = None  # id of the newer version, when known
    last_updated: str | None = None  # source's modified/updated timestamp (ISO 8601)
    croissant: dict[str, Any] | None = (
        None  # file-level Croissant export, on resolve(format=croissant)
    )
    ro_crate: dict[str, Any] | None = None  # RO-Crate export, on resolve(format=ro-crate)
    access_modes: list[str] = Field(default_factory=list)  # best-effort: fetch + operate modes
    mirrors: list[Mirror] = Field(
        default_factory=list
    )  # same-dataset copies folded by opt-in search collapse_mirrors; empty otherwise


class TaxonExpansion(BaseModel):
    """Echo of taxon-synonym expansion that fired for a search (transparency)."""

    input: str  # the organism param as given
    taxid: int
    canonical_name: str
    synonyms: list[str]  # names added to the query (excludes the canonical name)


class MeshExpansion(BaseModel):
    """Echo of MeSH-synonym expansion that fired for a search (transparency)."""

    input: str  # the disease param as given
    mesh_ui: str  # e.g. "D001943"
    canonical_name: str
    synonyms: list[str]  # entry terms added to the query (excludes the canonical name)


class TissueExpansion(BaseModel):
    """Echo of UBERON tissue-synonym expansion that fired for a search (transparency)."""

    input: str  # the tissue param as given
    uberon_id: str  # e.g. "UBERON:0002107"
    canonical_name: str
    synonyms: list[str]  # entry synonyms added to the query (excludes the canonical label)


class ChemicalExpansion(BaseModel):
    """Echo of ChEBI chemical-synonym expansion that fired for a search (transparency)."""

    input: str  # the chemical param as given
    chebi_id: str  # e.g. "CHEBI:27732"
    canonical_name: str
    synonyms: list[str]  # entry synonyms added to the query (excludes the canonical label)


class AssayExpansion(BaseModel):
    """Echo of EDAM assay-synonym expansion that fired for a search (transparency)."""

    input: str  # the assay param as given
    edam_id: str  # e.g. "EDAM:topic_3169"
    canonical_name: str
    synonyms: list[str]  # entry synonyms added to the query (excludes the canonical label)


class SearchResult(BaseModel):
    query: str
    total: int
    count: int
    results: list[DataResource] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)  # {source: message}
    next_cursor: str | None = None
    taxon_expansion: TaxonExpansion | None = None
    mesh_expansion: MeshExpansion | None = None
    tissue_expansion: TissueExpansion | None = None
    chemical_expansion: ChemicalExpansion | None = None
    assay_expansion: AssayExpansion | None = None


class FetchResult(BaseModel):
    paths: list[str] = Field(default_factory=list)
    bytes: int = 0
    skipped: list[str] = Field(default_factory=list)
    resumed: list[str] = Field(default_factory=list)


SEARCH_DESC_LIMIT = 500


def compact(r: DataResource) -> DataResource:
    """Search-result form of a resource: drop the file manifest and truncate
    the description. Token-budget rule — callers pull the full record and
    ``files[]`` via ``resolve``. Returns a copy; the input is not mutated.
    """
    desc = r.description[:SEARCH_DESC_LIMIT] if r.description else None
    return r.model_copy(update={"files": [], "description": desc})


_ACCESS_ALIASES = {
    "open": "open",
    "embargo": "embargoed",
    "embargoed": "embargoed",
    "restricted": "restricted",
    "closed": "closed",
}


def normalize_access(raw: str | None) -> str | None:
    """Map a source access token to the normalized vocabulary
    {open, embargoed, restricted, closed}; 'unknown' for an unrecognized
    non-empty value; None when absent. Case-insensitive."""
    if not raw or not str(raw).strip():
        return None
    return _ACCESS_ALIASES.get(str(raw).strip().lower(), "unknown")


# links[].rel values that say "a NEWER version of me exists" (I am superseded).
_SUPERSEDED_BY_RELS = {"is_previous_version_of", "is_obsoleted_by"}
# links[].rel values that say "I supersede / version an OLDER record".
_SUPERSEDES_RELS = {"is_new_version_of", "obsoletes", "has_version", "is_version_of"}


def derive_version_status(links: list[Link]) -> tuple[bool | None, str | None]:
    """Infer (is_latest, superseded_by) from version relations in links[].
    Returns (None, None) when links carry no version information at all —
    absence of evidence, not a claim of latest."""
    for lnk in links:
        if lnk.rel in _SUPERSEDED_BY_RELS:
            return False, lnk.target_id
    if any(lnk.rel in _SUPERSEDES_RELS for lnk in links):
        return True, None
    return None, None


_TABULAR_EXTS = (".parquet", ".pq", ".csv", ".tsv")


def derive_access_modes(files: list[FileEntry], *, operate: bool) -> list[str]:
    """Best-effort Tier-1 capability claim for a resolved record.

    ``fetch`` when any file has a download url; the operate modes
    (schema/preview/head/sql) when a tabular file is present AND the [operate]
    extra is installed. Format-dependent modes are *claims* — operate verifies
    them per-file and fails loud if the claim does not hold.
    """
    has_url = any(f.url for f in files)
    if not has_url:
        return []
    modes = ["fetch"]
    if operate and any((f.name or "").lower().endswith(_TABULAR_EXTS) for f in files if f.url):
        modes += ["schema", "preview", "head", "sql"]
    return modes
