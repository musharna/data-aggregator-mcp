"""B9 `relate` — pure, deterministic cross-resource join/harmonization hint detection.

`detect(resources)` reasons over the NORMALIZED metadata of already-resolved
DataResources and returns evidence-backed JoinHints for four strong structural signals.
NO network, NO file I/O, NO executed joins — it names a shared value and stops (the
HINTS-only boundary). The handler (`router.relate`) does the resolve fan-out.

Known limitations (matching is exact-string on normalized ids, by design):
- A version edge whose `superseded_by` / `links[].target_id` is a URL-form
  identifier (e.g. ``https://doi.org/10.5281/zenodo.1``) rather than a bare DOI or
  source-prefixed id will not match the address map, so the lineage/link hint is
  silently skipped — a best-effort false negative, never a wrong hint. (Zenodo
  emits bare DOIs and matches; some DataCite relatedIdentifiers are URLs.)
- A mutual `superseded_by` cycle (A→B and B→A — contradictory upstream metadata)
  yields a single hint whose [newer, older] direction is whichever resource is
  iterated first; the direction is not meaningful for a true cycle.
"""

from __future__ import annotations

from data_aggregator_mcp.models import DataResource, JoinHint


def _norm(value: str | None) -> str | None:
    """Case-insensitive, stripped key for exact-match comparison; None if empty."""
    if not value:
        return None
    s = value.strip().lower()
    return s or None


def detect(resources: list[DataResource]) -> list[JoinHint]:
    """All hints across `resources`. Order: accession, identifier, link, lineage."""
    hints: list[JoinHint] = []
    hints.extend(_shared_accession(resources))
    hints.extend(_shared_identifier(resources))
    hints.extend(_explicit_link(resources))
    hints.extend(_version_lineage(resources))
    return hints


def _shared_identifier(resources: list[DataResource]) -> list[JoinHint]:
    by_id: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for r in resources:
        values: set[str] = set()
        if r.doi:
            values.add(r.doi)
        for v in r.identifiers.values():
            if v:
                values.add(v)
        for v in values:
            n = _norm(v)
            if not n:
                continue
            display.setdefault(n, v)
            ids = by_id.setdefault(n, [])
            if r.id not in ids:  # one resource counts once per value -> no self-hint
                ids.append(r.id)
    hints: list[JoinHint] = []
    for n, ids in by_id.items():
        if len(ids) >= 2:
            hints.append(
                JoinHint(
                    kind="shared_identifier",
                    resources=ids,
                    key=display[n],
                    evidence=f"identifier {display[n]!r} shared by {len(ids)} resources",
                    suggestion=f"same work or paper-data link via {display[n]}",
                )
            )
    return hints


def _shared_accession(resources: list[DataResource]) -> list[JoinHint]:
    by_acc: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for r in resources:
        for acc in r.accessions:
            n = _norm(acc)
            if not n:
                continue
            display.setdefault(n, acc)
            ids = by_acc.setdefault(n, [])
            if r.id not in ids:
                ids.append(r.id)
    hints: list[JoinHint] = []
    for n, ids in by_acc.items():
        if len(ids) >= 2:
            hints.append(
                JoinHint(
                    kind="shared_accession",
                    resources=ids,
                    key=display[n],
                    evidence=f"accession {display[n]!r} present on {len(ids)} resources",
                    suggestion=f"joinable on accession {display[n]}",
                )
            )
    return hints


def _address_map(resources: list[DataResource], *, include_accessions: bool) -> dict[str, str]:
    """Map each resource's addressable ids (id, doi, optionally accessions), normalized,
    to the OWNING resource id. First writer wins on a collision (shared doi is handled by
    the identifier detector, not here)."""
    addr: dict[str, str] = {}
    for r in resources:
        candidates = [r.id, r.doi]
        if include_accessions:
            candidates += list(r.accessions)
        for c in candidates:
            n = _norm(c)
            if n:
                addr.setdefault(n, r.id)
    return addr


def _explicit_link(resources: list[DataResource]) -> list[JoinHint]:
    addr = _address_map(resources, include_accessions=True)
    hints: list[JoinHint] = []
    seen: set[tuple[str, str, str]] = set()
    for r in resources:
        for link in r.links:
            n = _norm(link.target_id)
            if not n:
                continue
            target = addr.get(n)
            if not target or target == r.id:
                continue
            dedup = (r.id, target, link.rel)
            if dedup in seen:
                continue
            seen.add(dedup)
            hints.append(
                JoinHint(
                    kind="explicit_link",
                    resources=[r.id, target],
                    key=link.rel,
                    evidence=f"{r.id} links to {target} via {link.rel!r} (target_id={link.target_id!r})",
                    suggestion=f"{r.id} {link.rel} {target} (declared in source metadata)",
                )
            )
    return hints


def _version_lineage(resources: list[DataResource]) -> list[JoinHint]:
    addr = _address_map(resources, include_accessions=False)
    hints: list[JoinHint] = []
    seen: set[tuple[str, str]] = set()
    for r in resources:
        raw = r.superseded_by
        if not raw:
            continue
        n = _norm(raw)
        if not n:
            continue
        newer = addr.get(n)  # the resource r.superseded_by points to
        if not newer or newer == r.id:
            continue
        dedup: tuple[str, str] = (r.id, newer) if r.id <= newer else (newer, r.id)
        if dedup in seen:
            continue
        seen.add(dedup)
        hints.append(
            JoinHint(
                kind="version_lineage",
                resources=[newer, r.id],  # [newer, older]
                key=raw,
                evidence=f"{r.id}.superseded_by -> {newer}",
                suggestion=f"{newer} is a newer version of {r.id} - dedupe, don't join, these",
            )
        )
    return hints
