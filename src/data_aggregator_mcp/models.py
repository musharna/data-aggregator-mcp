"""The wire contract — every source normalizes into DataResource."""

from __future__ import annotations

import re

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
    is_latest: bool | None = None  # None = no version info in links[]
    superseded_by: str | None = None  # id of the newer version, when known
    last_updated: str | None = None  # source's modified/updated timestamp (ISO 8601)


class TaxonExpansion(BaseModel):
    """Echo of taxon-synonym expansion that fired for a search (transparency)."""

    input: str  # the organism param as given
    taxid: int
    canonical_name: str
    synonyms: list[str]  # names added to the query (excludes the canonical name)


class SearchResult(BaseModel):
    query: str
    total: int
    count: int
    results: list[DataResource] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)  # {source: message}
    next_cursor: str | None = None
    taxon_expansion: TaxonExpansion | None = None


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


def derive_version_status(links: list["Link"]) -> tuple[bool | None, str | None]:
    """Infer (is_latest, superseded_by) from version relations in links[].
    Returns (None, None) when links carry no version information at all —
    absence of evidence, not a claim of latest."""
    for lnk in links:
        if lnk.rel in _SUPERSEDED_BY_RELS:
            return False, lnk.target_id
    if any(lnk.rel in _SUPERSEDES_RELS for lnk in links):
        return True, None
    return None, None
