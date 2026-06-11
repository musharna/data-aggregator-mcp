"""B9 `relate` — pure, deterministic cross-resource join/harmonization hint detection.

`detect(resources)` reasons over the NORMALIZED metadata of already-resolved
DataResources and returns evidence-backed JoinHints for four strong structural signals.
NO network, NO file I/O, NO executed joins — it names a shared value and stops (the
HINTS-only boundary). The handler (`router.relate`) does the resolve fan-out.
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
