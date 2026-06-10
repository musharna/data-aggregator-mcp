"""FAIR-score enricher — a PURE function over a normalized ``DataResource``.

``assess(resource)`` computes a 0–100 FAIRness score plus F/A/I/R sub-scores and
a list of actionable gaps, grounded in the **RDA FAIR Data Maturity Model**
(Specification & Guidelines v0.90, RD-Alliance 2020). Each indicator carries its
RDA id (e.g. ``RDA-F1-01D``) and priority (Essential/Important/Useful); gaps name
that id and are framed as metadata-exposure gaps ("metadata does not expose X"),
never value judgements about the dataset.

This module implements only the MACHINE-EVALUABLE SUBSET — indicators computable
from the metadata we already hold. Indicators that would require fetching the data
or external probes are deliberately OUT: we never fabricate a pass/fail for what we
cannot see. ``assessed`` reports exactly how many indicators were evaluated.

PURE: no network, no file I/O, deterministic. Unlike ``trust.annotate`` (which calls
Crossref), ``assess`` takes only the resource — there is no client argument.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from data_aggregator_mcp.models import DataResource, FairAssessment

# Priority weights from the RDA Maturity Model.
ESSENTIAL = 3
IMPORTANT = 2
USEFUL = 1

# Known scientific/community-standard file extensions (R1.3) — a non-exhaustive,
# honest set of formats with a recognised representation in research data.
_KNOWN_EXTS = (
    ".csv",
    ".tsv",
    ".parquet",
    ".pq",
    ".json",
    ".jsonl",
    ".xml",
    ".h5",
    ".hdf5",
    ".nc",
    ".cdf",
    ".fasta",
    ".fa",
    ".fastq",
    ".fq",
    ".bam",
    ".sam",
    ".vcf",
    ".cram",
    ".bed",
    ".gff",
    ".gff3",
    ".gtf",
    ".cif",
    ".pdb",
    ".mzml",
    ".mzxml",
    ".nwb",
    ".h5ad",
    ".rds",
    ".arff",
    ".tif",
    ".tiff",
    ".obo",
    ".owl",
)

# License-family tokens that signal a machine-understandable reuse licence (R1.1b).
# Matched as WHOLE tokens, never as substrings — substring matching is the trap
# ("mit" ⊂ "submitted", "by" ⊂ free prose), which would mark free text as a
# machine id. A free-text licence ("see LICENSE.txt", "Contact authors") passes
# R1.1a (a licence IS present) but must fail R1.1b (it is not a machine id).
_LICENSE_FAMILIES = frozenset(
    {
        "cc",
        "cc0",
        "mit",
        "apache",
        "gpl",
        "lgpl",
        "agpl",
        "bsd",
        "mpl",
        "epl",
        "odbl",
        "odc",
        "pddl",
        "unlicense",
        "zlib",
    }
)
# Distinctive multi-word/URL signals safe to match as substrings.
_LICENSE_PHRASES = ("creativecommons", "public domain", "spdx.org")
# Token split on anything that is not an SPDX id char (keep . and + for "4.0", "gpl+").
_LICENSE_TOKEN_RE = re.compile(r"[^a-z0-9.+]+")


def _machine_readable_license(license_str: str | None) -> bool:
    """True when the licence string looks like a known SPDX/CC machine-readable id
    or a licence URL, rather than free prose. A family token must appear as a WHOLE
    token (not a substring), OR the whole string is a compact SPDX-shaped id."""
    if not license_str:
        return False
    low = license_str.strip().lower()
    if any(p in low for p in _LICENSE_PHRASES):
        return True
    tokens = {t for t in _LICENSE_TOKEN_RE.split(low) if t}
    if tokens & _LICENSE_FAMILIES:
        return True
    # SPDX-shaped: a compact id token with no spaces, carrying a version/variant marker.
    return " " not in low and len(low) <= 40 and ("-" in low or any(c.isdigit() for c in low))


@dataclass(frozen=True)
class Indicator:
    """One machine-evaluable RDA Maturity-Model indicator."""

    dim: str  # "findable" | "accessible" | "interoperable" | "reusable"
    rda_id: str  # e.g. "RDA-F1-01D"
    weight: int  # ESSENTIAL / IMPORTANT / USEFUL
    predicate: Callable[[DataResource], bool]
    gap: str  # human-readable gap message (already includes the RDA id)


# --- predicates (each reads only fields the metadata actually holds) ---------


def _has_doi(r: DataResource) -> bool:
    return bool(r.doi or r.identifiers.get("doi"))


def _rich_metadata(r: DataResource) -> bool:
    return bool(r.title) and bool(r.description) and bool(r.creators or r.subjects)


def _has_data_identifier(r: DataResource) -> bool:
    return bool(r.doi or r.accessions or r.identifiers)


def _retrievable(r: DataResource) -> bool:
    return _has_doi(r) or any(f.url for f in r.files)


def _open_protocol(r: DataResource) -> bool:
    return any((f.url or "").startswith("https://") for f in r.files) or _has_doi(r)


def _has_file_format(r: DataResource) -> bool:
    return any(f.mime for f in r.files)


def _has_vocab(r: DataResource) -> bool:
    return bool(r.taxa) or bool(r.subjects)


def _has_links(r: DataResource) -> bool:
    return bool(r.links)


def _has_license(r: DataResource) -> bool:
    return bool(r.license)


def _machine_license(r: DataResource) -> bool:
    return _machine_readable_license(r.license)


def _provenance(r: DataResource) -> bool:
    return bool(r.creators) and bool(r.funding or r.last_updated or r.links or r.source)


def _community_standard(r: DataResource) -> bool:
    if r.accessions:
        return True
    return any((f.name or "").lower().endswith(_KNOWN_EXTS) for f in r.files)


# F4 / A2 are justified constants, NOT freebies:
#   F4 — the record came from a searchable registry / our fan-out, so it IS
#        indexed in a searchable resource by construction.
#   A2 — our metadata-bearing sources (DataCite, registries) keep metadata after
#        data withdrawal, so metadata persists independently of the data.
def _true(_r: DataResource) -> bool:
    return True


INDICATORS: tuple[Indicator, ...] = (
    # FINDABLE
    Indicator(
        "findable",
        "RDA-F1-01D",
        ESSENTIAL,
        _has_doi,
        "no DOI/persistent identifier (RDA-F1-01D)",
    ),
    Indicator(
        "findable",
        "RDA-F2-01M",
        ESSENTIAL,
        _rich_metadata,
        "sparse metadata: needs description + creators/subjects (RDA-F2-01M)",
    ),
    Indicator(
        "findable",
        "RDA-F3-01M",
        IMPORTANT,
        _has_data_identifier,
        "metadata exposes no resolvable data identifier (RDA-F3-01M)",
    ),
    Indicator(
        "findable",
        "RDA-F4-01M",
        IMPORTANT,
        _true,  # record came from a searchable registry/our fan-out (justified constant)
        "metadata is not indexed in a searchable resource (RDA-F4-01M)",
    ),
    # ACCESSIBLE
    Indicator(
        "accessible",
        "RDA-A1-01M",
        ESSENTIAL,
        _retrievable,
        "no resolvable identifier or download URL (RDA-A1-01M)",
    ),
    Indicator(
        "accessible",
        "RDA-A1.1-01M",
        IMPORTANT,
        _open_protocol,
        "no open (https/doi) access protocol (RDA-A1.1-01M)",
    ),
    Indicator(
        "accessible",
        "RDA-A2-01M",
        IMPORTANT,
        _true,  # registry-backed metadata persists independently of the data (justified constant)
        "metadata does not persist independently of the data (RDA-A2-01M)",
    ),
    # INTEROPERABLE
    Indicator(
        "interoperable",
        "RDA-I1-01M",
        ESSENTIAL,
        _has_file_format,
        "no machine-readable file formats declared (RDA-I1-01M)",
    ),
    Indicator(
        "interoperable",
        "RDA-I2-01M",
        USEFUL,
        _has_vocab,
        "no controlled-vocabulary terms (taxa/subjects) (RDA-I2-01M)",
    ),
    Indicator(
        "interoperable",
        "RDA-I3-01M",
        IMPORTANT,
        _has_links,
        "no qualified links to related records (RDA-I3-01M)",
    ),
    # REUSABLE
    Indicator(
        "reusable",
        "RDA-R1.1-01M",
        ESSENTIAL,
        _has_license,
        "no reuse licence (RDA-R1.1-01M)",
    ),
    Indicator(
        "reusable",
        "RDA-R1.1-03M",
        IMPORTANT,
        _machine_license,
        "licence is free text, not a machine-readable id (RDA-R1.1-03M)",
    ),
    Indicator(
        "reusable",
        "RDA-R1.2-01M",
        IMPORTANT,
        _provenance,
        "thin provenance: needs creators + (funding/dateModified/relations) (RDA-R1.2-01M)",
    ),
    Indicator(
        "reusable",
        "RDA-R1.3-01M",
        USEFUL,
        _community_standard,
        "no recognised community-standard format (RDA-R1.3-01M)",
    ),
)

_DIMENSIONS = ("findable", "accessible", "interoperable", "reusable")


def assess(resource: DataResource) -> FairAssessment:
    """Compute the RDA-grounded FAIRness assessment for a resource. PURE: no I/O,
    deterministic. Per-dimension score = round(100 * passed-weight / total-weight);
    overall = round(mean of the 4 dimension scores)."""
    passed_w: dict[str, int] = dict.fromkeys(_DIMENSIONS, 0)
    total_w: dict[str, int] = dict.fromkeys(_DIMENSIONS, 0)
    gaps: list[str] = []

    for ind in INDICATORS:
        total_w[ind.dim] += ind.weight
        if ind.predicate(resource):
            passed_w[ind.dim] += ind.weight
        else:
            gaps.append(ind.gap)

    dim_scores = {
        dim: round(100 * passed_w[dim] / total_w[dim]) if total_w[dim] else 0 for dim in _DIMENSIONS
    }
    overall = round(sum(dim_scores[dim] for dim in _DIMENSIONS) / len(_DIMENSIONS))

    return FairAssessment(
        score=overall,
        findable=dim_scores["findable"],
        accessible=dim_scores["accessible"],
        interoperable=dim_scores["interoperable"],
        reusable=dim_scores["reusable"],
        assessed=len(INDICATORS),
        gaps=gaps,
    )
