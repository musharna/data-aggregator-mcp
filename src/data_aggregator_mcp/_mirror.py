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

import re

from data_aggregator_mcp import sources
from data_aggregator_mcp.models import DataResource, Mirror

# Sources with no fetch backend (discovery-only) — lowest DOI-dedup precedence. Derived
# from the central registry, which also feeds the fetch gate, so the two cannot drift.
DISCOVERY_ONLY_SOURCES: frozenset[str] = sources.DISCOVERY_ONLY


def fetch_priority(r: DataResource) -> int:
    """DOI-collision precedence: higher wins. A fetchable copy must beat a discovery-only
    one (which carries no bytes at all), and a native fetch backend beats a DataCite record
    (whose fetchability is only host-detected on resolve). Keying on real fetchability —
    not the ``datacite:`` prefix — is what stops a discovery-only source (nasacmr/gwas) that
    happens to share a DOI (e.g. ORNL DAAC records held by both CMR and DataONE) from
    shadowing the verified fetchable copy purely by interleave position."""
    if r.source in DISCOVERY_ONLY_SOURCES:
        return 0
    if r.id.startswith("datacite:"):
        return 1
    return 2


def dedup_by_doi(resources: list[DataResource]) -> list[DataResource]:
    """Dedup by lowercased DOI, preserving first-seen order. On collision the
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
        key = r.doi.lower()
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


def first_author_surname(r: DataResource) -> str | None:
    """Lowercased last whitespace token of the first creator's name, or None if
    the record has no creators (then the title+author+year path cannot fire)."""
    if not r.creators:
        return None
    name = r.creators[0].name.strip()
    if not name:
        return None
    return name.split()[-1].lower()


def fingerprint_key(r: DataResource) -> tuple[str, str, int] | None:
    """``(normalized_title, first_author_surname, year)`` ONLY when all three are
    present/non-empty; else None (so a missing field can never satisfy the title
    path). Conservative content-identity key."""
    title = normalize_title(r.title) if r.title else ""
    surname = first_author_surname(r)
    if not title or not surname or r.year is None:
        return None
    return (title, surname, r.year)


def checksums(r: DataResource) -> set[str]:
    """Full ``algo:hex`` checksum strings present on a record's files (byte-level
    identity signal)."""
    return {f.checksum for f in r.files if f.checksum}


def survivor_rank(r: DataResource) -> tuple[int, int]:
    """Lower sorts first = better survivor. DOI-bearing beats DOI-less; among
    DOI-bearing, a native id (not ``datacite:``-prefixed) beats a ``datacite:``
    one — same precedence spirit as ``dedup_by_doi``. Ties fall through to first-seen
    order (stable sort on the group's encounter order)."""
    has_doi = 0 if r.doi else 1
    is_datacite = 1 if r.id.startswith("datacite:") else 0
    return (has_doi, is_datacite)


def collapse_mirrors(records: list[DataResource]) -> list[DataResource]:
    """Conservative, PURE content-dedup ON TOP OF exact-DOI dedup. Groups records
    that are the SAME dataset under different/no DOIs (a cross-repo mirror), folds
    each group to one survivor, and annotates the survivor's ``mirrors[]`` with the
    other members.

    A record joins a group iff it shares ANY full ``algo:hex`` file checksum with a
    member (byte-identical → definitional identity, source-agnostic) OR has the same
    ``fingerprint_key`` (normalized-title + first-author-surname + year, all present)
    as a member AND comes from a DIFFERENT source than every member already in that
    group. Title-only or partial matches never merge.

    The CROSS-SOURCE requirement on the fingerprint path is load-bearing: B7 is
    *cross-repo* dedup. Two same-source records that share title+author+year are
    almost always VERSION SIBLINGS (e.g. Zenodo record v1/v2), a relationship already
    modeled by ``is_latest``/``superseded_by`` (B1) — folding them as "mirrors" would
    be wrong. Only a copy in a DIFFERENT repository is a mirror. (Byte-identical
    checksums still fold regardless of source: identical bytes are the same data, and
    version siblings differ in bytes so they do not collide on the checksum path.)

    Survivor selection is deterministic (``survivor_rank`` + first-seen order). The
    survivor's ``mirrors`` lists every OTHER group member as ``Mirror(source,id,doi)``;
    a record is never its own mirror. First-seen order of survivors is preserved.
    Deterministic, no I/O.
    """

    class _Group:
        __slots__ = ("members", "keys", "checksums", "sources", "order")

        def __init__(self, order: int) -> None:
            self.members: list[DataResource] = []
            self.keys: set[tuple[str, str, int]] = set()
            self.checksums: set[str] = set()
            self.sources: set[str] = set()
            self.order = order

    groups: list[_Group] = []
    for r in records:
        key = fingerprint_key(r)
        sums = checksums(r)
        target: _Group | None = None
        for g in groups:
            checksum_hit = bool(sums & g.checksums)
            # Fingerprint match only counts CROSS-source — a same-source title+author+
            # year match is a version sibling (B1's domain), not a cross-repo mirror.
            fingerprint_hit = key is not None and key in g.keys and r.source not in g.sources
            if checksum_hit or fingerprint_hit:
                target = g
                break
        if target is None:
            target = _Group(len(groups))
            groups.append(target)
        target.members.append(r)
        if key is not None:
            target.keys.add(key)
        target.checksums |= sums
        target.sources.add(r.source)

    # Post-pass: union groups that share any checksum or fingerprint key, iterating
    # to fixpoint. The forward pass above uses greedy first-match, which misses
    # transitive connections: e.g. A(md5:X), B(sha:Y), C(md5:X + sha:Y) arriving
    # in order A,B,C — C joins A's group via md5:X, but B is stranded even though
    # it shares sha:Y with C. The union pass merges those stranded groups.
    changed = True
    while changed:
        changed = False
        merged_groups: list[_Group] = []
        for g in groups:
            absorbed = False
            for mg in merged_groups:
                checksum_overlap = bool(g.checksums & mg.checksums)
                # Fingerprint merge: any shared key where NOT all members share the
                # same source (the cross-source guard still applies globally — if two
                # groups have the same fingerprint key but all members come from the
                # same source, they are version siblings and must not be merged).
                key_overlap = bool(g.keys & mg.keys) and not (
                    g.sources <= mg.sources and len(g.sources) == 1 and g.sources == mg.sources
                )
                if checksum_overlap or key_overlap:
                    mg.members.extend(g.members)
                    mg.keys |= g.keys
                    mg.checksums |= g.checksums
                    mg.sources |= g.sources
                    absorbed = True
                    changed = True
                    break
            if not absorbed:
                merged_groups.append(g)
        groups = merged_groups

    out: list[DataResource] = []
    for g in groups:
        if len(g.members) == 1:
            out.append(g.members[0])
            continue
        # Stable pick: best rank wins, first-seen order breaks ties.
        survivor = min(enumerate(g.members), key=lambda im: (survivor_rank(im[1]), im[0]))[1]
        mirrors = [
            Mirror(source=m.source, id=m.id, doi=m.doi) for m in g.members if m is not survivor
        ]
        out.append(survivor.model_copy(update={"mirrors": mirrors}))
    return out
