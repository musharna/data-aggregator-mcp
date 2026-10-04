"""Cross-source record identity: exact-DOI dedup and conservative mirror collapse.

Pure and deterministic — no I/O, no adapter knowledge beyond the registry's
discovery-only set. Extracted from ``router`` because it answers a self-contained
question ("are these two records the same dataset?") that the orchestrator only
calls into; keeping it here lets the merge policy be read and tested without
wading through the fan-out.

Two layers, applied in order by the router:

1. :func:`dedup_by_doi` — exact DOI equality. Cheap and certain.
2. :func:`collapse_mirrors` — opt-in content dedup ON TOP of that, for the same
   dataset deposited in several repos under different (or no) DOIs.

Deliberately kept separate from ``_merge.interleave``, which is generic over any
element type and shared with multi-db adapters; everything here is specific to
``DataResource``.
"""

from __future__ import annotations

import itertools
import re

from data_aggregator_mcp import sources
from data_aggregator_mcp.models import DataResource, Mirror

# Sources with no fetch backend (discovery-only) — lowest DOI-dedup precedence. Derived
# from the central registry, which also feeds the fetch gate, so the two cannot drift.
DISCOVERY_ONLY_SOURCES: frozenset[str] = sources.DISCOVERY_ONLY

# ``fetch_priority`` values, lowest first; only their order matters.
DISCOVERY_ONLY_PRIORITY, DATACITE_PRIORITY, NATIVE_PRIORITY = range(3)


def fetch_priority(r: DataResource) -> int:
    """DOI-collision precedence: higher wins. A fetchable copy must beat a discovery-only
    one (which carries no bytes at all), and a native fetch backend beats a DataCite record
    (whose fetchability is only host-detected on resolve). Keying on real fetchability —
    not the ``datacite:`` prefix — is what stops a discovery-only source (nasacmr/gwas) that
    happens to share a DOI (e.g. ORNL DAAC records held by both CMR and DataONE) from
    shadowing the verified fetchable copy purely by interleave position."""
    if r.source in DISCOVERY_ONLY_SOURCES:
        return DISCOVERY_ONLY_PRIORITY
    if r.id.startswith("datacite:"):
        return DATACITE_PRIORITY
    return NATIVE_PRIORITY


def dedup_by_doi(resources: list[DataResource]) -> list[DataResource]:
    """Dedup by case-folded DOI (DOIs are case-insensitive), preserving first-seen order. On collision the
    higher-``fetch_priority`` record wins (fetchable native > DataCite > discovery-only),
    so the fetchable copy survives regardless of encounter order; ties keep the first seen.
    Records without a DOI are always kept.
    """
    by_doi: dict[str, DataResource] = {}
    order: list[str] = []
    no_doi: list[DataResource] = []
    for r in resources:
        if not r.doi:
            no_doi.append(r)
            continue
        key = r.doi.casefold()
        existing = by_doi.get(key)
        if existing is None:
            by_doi[key] = r
            order.append(key)
        elif fetch_priority(r) > fetch_priority(existing):
            by_doi[key] = r
    return [by_doi[k] for k in order] + no_doi


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Compared for EXACT
    normalized equality (never substring, never fuzzy) — the conservative
    content-dedup title key."""
    lowered = _PUNCT_RE.sub(" ", title.lower())
    return _WS_RE.sub(" ", lowered).strip()


def first_author_name_key(r: DataResource) -> str | None:
    """The first creator's whole name as its sorted lowercase words, or None if the
    record has no creators or the name has no words (then the title+author+year path
    cannot fire).

    Sorting makes the two written orders agree: DataCite and Zenodo write
    "Family, Given" ("Singh, Apoorv"), other repositories "Given Family" ("Apoorv
    Singh"). The whole name, not one token of it, is compared: a last-token key read
    the GIVEN name of every "Family, Given" creator (81% of 3,999 DataCite first
    creators sampled), so distinct datasets that share a generic title and year
    folded on a shared given name ("Zhang, rui" / "Zhe, Rui"), and a family-name key
    folds them on a shared surname ("Liu, Ziwei" / "LIU, Shuai")."""
    if not r.creators:
        return None
    return " ".join(sorted(normalize_title(r.creators[0].name).split())) or None


def fingerprint_key(r: DataResource) -> tuple[str, str, int] | None:
    """``(normalized_title, first_author_name_key, year)`` ONLY when all three are
    present/non-empty; else None (so a missing field can never satisfy the title
    path). Conservative content-identity key."""
    title = normalize_title(r.title)
    author = first_author_name_key(r)
    if not title or not author or r.year is None:
        return None
    return (title, author, r.year)


def checksums(r: DataResource) -> set[str]:
    """Full ``algo:hex`` checksum strings present on a record's files (byte-level
    identity signal)."""
    return {f.checksum for f in r.files if f.checksum}


def survivor_rank(r: DataResource) -> tuple[bool, bool]:
    """Lower sorts first = better survivor (``False`` < ``True``). DOI-bearing beats
    DOI-less; among DOI-bearing, a native id (not ``datacite:``-prefixed) beats a
    ``datacite:`` one — same precedence spirit as ``dedup_by_doi``. Ties go to the
    earliest record (``collapse_mirrors``)."""
    return (not r.doi, r.id.startswith("datacite:"))


def _representative_rank(r: DataResource) -> tuple[bool, bool, bool, str]:
    """Which of one source's records in a group stands for the deposit (lower first):
    the better ``survivor_rank``, then the latest version, then the smaller id, so the
    choice does not depend on the order the records arrive in."""
    return (*survivor_rank(r), r.is_latest is not True, r.id)


def collapse_mirrors(records: list[DataResource]) -> list[DataResource]:
    """Conservative, PURE content-dedup ON TOP OF exact-DOI dedup. Groups records
    that are the SAME dataset under different/no DOIs (a cross-repo mirror), folds
    each group to one survivor, and annotates the survivor's ``mirrors[]`` with the
    other members.

    A mirror is a copy in ANOTHER repository, so only records of different sources
    match: two share ANY full ``algo:hex`` file checksum (byte-identical → the same
    data) OR the same ``fingerprint_key`` (normalized-title + first-author name +
    year, all present). Title-only or partial matches never match. Matches chain
    (A~B, B~C puts A, B, C in one group).

    Records of one source never fold into each other, whatever they share (user
    decision 2026-10-04): they are versions of one deposit (Zenodo v1/v2, or through
    DataCite a concept DOI and its version DOIs), which ``is_latest``/``superseded_by``
    describe, and they can share unchanged files byte for byte. A copy in another
    repository mirrors the deposit, so when a group holds several records of one
    source, only that source's representative (``_representative_rank``) folds; the
    deposit's other records stay separate hits. (Zenodo 11459539, its concept DOI
    through DataCite and its ResearchGate copy: the copy folds under 11459539, the
    concept DOI stays a hit.)

    The survivor is the member with the best ``survivor_rank``, ties going to the
    earliest in ``records``; its ``mirrors`` lists every OTHER member, in ``records``
    order, as ``Mirror(source,id,doi)``. Survivors come out in the order of each
    group's earliest record, and the groups do not depend on that order.
    Deterministic, no I/O.
    """
    keys = [fingerprint_key(r) for r in records]
    sums = [checksums(r) for r in records]
    parent = list(range(len(records)))

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(len(records)), 2):
        if records[i].source == records[j].source:
            continue
        if sums[i] & sums[j] or (keys[i] is not None and keys[i] == keys[j]):
            parent[max(root(i), root(j))] = min(root(i), root(j))
    members: dict[int, list[int]] = {}
    for i in range(len(records)):
        members.setdefault(root(i), []).append(i)
    groups: list[list[int]] = []
    for g in members.values():
        by_source: dict[str, list[int]] = {}
        for i in g:
            by_source.setdefault(records[i].source, []).append(i)
        reps = sorted(
            min(ids, key=lambda i: _representative_rank(records[i])) for ids in by_source.values()
        )
        groups.append(reps)
        groups.extend([i] for i in g if i not in reps)
    # Each group is ascending and no two share a record, so this is earliest-record order.
    groups.sort()

    out: list[DataResource] = []
    for g in groups:
        best = min(g, key=lambda i: (survivor_rank(records[i]), i))
        mirrors = [
            Mirror(source=records[i].source, id=records[i].id, doi=records[i].doi)
            for i in sorted(g)
            if i != best
        ]
        out.append(records[best].model_copy(update={"mirrors": mirrors}))
    return out
