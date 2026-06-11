"""B9 `relate` — pure, deterministic cross-resource join/harmonization hint detection.

`detect(resources)` reasons over the NORMALIZED metadata of already-resolved
DataResources and returns evidence-backed JoinHints for four strong structural signals.
NO network, NO file I/O, NO executed joins — it names a shared value and stops (the
HINTS-only boundary). The handler (`router.relate`) does the resolve fan-out.

Matching is exact-string on `_norm`-canonicalized ids. `_norm` folds DOI resolver/
scheme forms (``https://doi.org/10.x``, ``doi:10.x``, bare ``10.x``) together, so a
DOI matches across representations. A `superseded_by` cycle (contradictory upstream
metadata) is detected and reported as a contradiction rather than an asserted order.

Remaining limitation: a target id given only as a source-specific *record* URL
(e.g. ``https://zenodo.org/record/2`` instead of ``zenodo:2`` or its DOI) is not
mapped to the owning resource — that needs per-source URL parsing, out of scope here.
A miss is a best-effort false negative, never a wrong hint.
"""

from __future__ import annotations

from data_aggregator_mcp.models import DataResource, JoinHint

# DOI resolver / scheme forms that all denote the same DOI. Stripped (after
# lower-casing) so a bare DOI, a `doi:`-scheme DOI, and a resolver-URL DOI compare
# equal. No source uses `doi:` as an id prefix (ids are `<source>:<localpart>`, e.g.
# `zenodo:2`, `datacite:10.x`), so this never collides with ids or accessions.
_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "dx.doi.org/",
    "doi:",
)


def _norm(value: str | None) -> str | None:
    """Case-insensitive, stripped, DOI-canonicalized key for exact-match comparison;
    None if empty. DOI resolver/scheme prefixes are removed so the same DOI matches
    across its bare / `doi:` / `https://doi.org/` representations."""
    if not value:
        return None
    s = value.strip().lower()
    if not s:
        return None
    for prefix in _DOI_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
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
    # Collect resolved directed edges older -> newer (newer is claimed newer than older,
    # via older.superseded_by). A well-formed version graph is a DAG; a cycle means
    # contradictory upstream metadata, which we report as such rather than inventing a
    # direction.
    edges: list[tuple[str, str, str]] = []  # (older, newer, raw_key)
    succ: dict[str, set[str]] = {}
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
        edges.append((r.id, newer, raw))
        succ.setdefault(newer, set())
        succ.setdefault(r.id, set()).add(newer)

    def _reaches(start: str, target: str) -> bool:
        """True if `target` is reachable from `start` following superseded_by edges —
        i.e. start's lineage loops back to target, so the pair sits on a cycle."""
        stack = [start]
        visited: set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(succ.get(node, ()))
        return False

    hints: list[JoinHint] = []
    seen: set[tuple[str, str]] = set()
    for older, newer, raw in edges:
        pair = (older, newer) if older <= newer else (newer, older)
        if pair in seen:
            continue
        seen.add(pair)
        if _reaches(newer, older):  # newer transitively claims older is newer -> cycle
            a, b = pair
            hints.append(
                JoinHint(
                    kind="version_lineage",
                    resources=[a, b],  # sorted; no direction is meaningful in a cycle
                    key=raw,
                    evidence=f"{older} and {newer} sit on a superseded_by cycle "
                    "(each is transitively claimed newer than the other)",
                    suggestion=f"contradictory version metadata linking {a} and {b} - "
                    "resolve upstream; a newer/older direction cannot be inferred",
                )
            )
        else:
            hints.append(
                JoinHint(
                    kind="version_lineage",
                    resources=[newer, older],  # [newer, older]
                    key=raw,
                    evidence=f"{older}.superseded_by -> {newer}",
                    suggestion=f"{newer} is a newer version of {older} - dedupe, don't join, these",
                )
            )
    return hints
