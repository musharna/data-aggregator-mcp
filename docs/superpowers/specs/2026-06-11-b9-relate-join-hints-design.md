# B9 — `relate`: cross-resource join/harmonization hints

**Status:** approved 2026-06-11 · **Roadmap:** v4 competitive analysis, Tier-2 "the one net-new feature worth it now"

## Goal

Give an agent a way to ask "how do these datasets relate / can I join them?" across a
**set** of resources, and get back precise, evidence-backed **hints** — without the
server executing any join, reading any file column, or fabricating a relationship.

This is the first **cross-resource** tool (every existing tool — search/resolve/fetch/
operate — is single-query or single-record). It is structurally hard for a breadth
gateway to copy because it reasons over the _normalized, resolved_ metadata the
aggregator already produces.

## Scope decisions (locked during brainstorming 2026-06-11)

1. **Metadata-level, not column-level.** Hints come from metadata the aggregator already
   normalizes (accessions, cross-identifiers, links, version chains). It does **not**
   read tabular file schemas or compare columns (that would need per-file range-reads,
   tabular-only, plus ID-class heuristics — deferred, see Non-goals).
2. **New tool taking explicit ids**, not a flag on `search` and not a mode on `operate`.
   Join-relevant fields (`links`, `identifiers`, `accessions`) populate mostly on
   **resolve**, so the tool resolves each input id internally (TTL-cached).
3. **Strong structural signals only** — high precision, low noise. No weak
   shared-context signals (organism/subjects), which fire on almost everything.

## Tool surface

```
relate(ids: list[str]) -> RelateResult
```

- `ids`: **2–10** source-prefixed resource ids (e.g. `["geo:GSE12345", "sra:SRP000001"]`).
  - `< 2` ids → `ValidationError` ("relate needs at least 2 ids").
  - `> 10` ids → `ValidationError` (bounds the internal resolve cost; matches the
    server's existing explicit-cap style). 10 is a deliberate cap, not a paged window.
- **Behavior:** resolve each id concurrently via the existing `router.resolve` path
  (TTL-cached; no new network surface). A per-id resolve failure is recorded in
  `errors` and that id is skipped — fail-soft, never raises for one bad id. If fewer
  than 2 ids resolve, return an empty `hints` list with an explanatory `note` (no error).
- **Output:** a `RelateResult` (below). Pure metadata reasoning; no file fetch, no
  column read, no executed join.

## Detection — the four signals

A **pure, deterministic** function `detect(resources: list[DataResource]) -> list[JoinHint]`
(no I/O) scans the resolved records. A hint fires only when a signal links **≥2** of the
input resources, and every hint names the shared **value** as evidence.

| `kind`              | fires when                                                                                                                              | `suggestion` (hint text)                                        |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `shared_accession`  | the same accession (BioProject/SRA/GEO/…) appears in `accessions` of ≥2 resources                                                       | "joinable on accession `<value>`"                               |
| `shared_identifier` | the same cross-identifier (doi / pmid / pmcid) appears across ≥2 resources (`doi` field + `identifiers` dict)                           | "same work or paper↔data link via `<value>`"                    |
| `explicit_link`     | one resource's `links[].target_id` matches another input resource's `id`, `doi`, or an `accession`                                      | "`<A>` `<rel>` `<B>` (declared in source metadata)"             |
| `version_lineage`   | one resource's `superseded_by` equals another input resource's `id` or `doi` (a directed newer→older version edge within the input set) | "`<A>` is a newer version of `<B>` — dedupe, don't join, these" |

Matching rules (to avoid false hints):

- Identifier/accession comparison is **case-insensitive**, exact-string (a normalized
  DOI vs DOI; an accession token vs accession token). No fuzzy/substring matching.
- A resource is never related **to itself**; a hint's `resources` list holds ≥2 _distinct_
  input ids.
- The **same** shared value collapses into **one** hint listing all resources that carry
  it (e.g. three datasets sharing `PRJNA123` → one `shared_accession` hint with three ids),
  not pairwise duplicates.
- A resource's own `doi` is matched against _other_ resources' `doi`/`identifiers` — a
  resource carrying its own DOI in both its `doi` field and `identifiers` is not a match
  against itself.

## Output model (new in `models.py`)

```python
class JoinHint(BaseModel):
    kind: Literal["shared_accession", "shared_identifier", "explicit_link", "version_lineage"]
    resources: list[str]   # ≥2 distinct input ids this hint connects
    key: str               # the shared value / relation (the accession, the doi, the rel)
    evidence: str          # what was matched and where (e.g. "accessions[] on both")
    suggestion: str        # human/agent-readable HINT; never an executed action

class RelateResult(BaseModel):
    input_ids: list[str]            # as given
    resolved: list[str]             # ids that resolved successfully
    hints: list[JoinHint]           # may be empty
    errors: dict[str, str] = {}     # id -> resolve-failure reason (fail-soft)
    note: str | None = None         # e.g. "no structural relationships detected among 4 resources"
```

`note` is set when `hints` is empty: distinguishing "we looked and found nothing" from
"something errored" (honesty — same discipline as `dossier`/FAIR "unknown, not absent").

## The HINTS-only boundary (the heart of this wave)

`relate` points at a join key and stops. It explicitly does **not**:

- fetch or download any file,
- read or compare tabular columns,
- run any join, merge, or ID conversion,
- assert a relationship that isn't grounded in a shared metadata value.

This keeps `relate` inside the project's ceded "no structured-annotation execution"
boundary (structured bio-DB annotation / join execution → sibling tools), and makes the
hint trustworthy: every hint is backed by a literal shared value the caller can verify.

## Architecture / files

Follows the established **pure-renderer + I/O-in-handler** pattern (mirrors `dossier.py`):

- **`src/data_aggregator_mcp/relate.py`** — new. Pure `detect(resources) -> list[JoinHint]`.
  No network, no file I/O, deterministic. Holds the four detectors + the
  collapse-by-shared-value logic. Fully unit-testable from fixtures.
- **`src/data_aggregator_mcp/router.py`** — thin `relate(client, ids)` orchestrator:
  validate count → resolve each id concurrently (existing resolve path, cached,
  per-id try/except into `errors`) → call `relate.detect(resolved)` → assemble
  `RelateResult` (+ `note` when empty).
- **`src/data_aggregator_mcp/models.py`** — add `JoinHint`, `RelateResult`.
- **`src/data_aggregator_mcp/server.py`** — register the 6th tool `relate` (name, JSON
  schema: `ids` array minItems 2 maxItems 10, description stating HINTS-only), wire the
  handler to `router.relate`, serialize `RelateResult`.
- **`README.md`** — document `relate` under the tool list (HINTS-only; metadata-level).
- **`CHANGELOG.md`** — `Added` entry under a new version section at release.

No new dependencies (pure stdlib + existing models). No new network endpoints.

## Error handling

- `< 2` or `> 10` ids → `ValidationError` (the only hard errors).
- Per-id resolve failure → recorded in `errors[id]`, id skipped, processing continues.
- `< 2` resolved → empty `hints` + `note`, **not** an error (the inputs were valid).
- `detect` never raises: a missing/empty field simply contributes no hint.

## Testing

**Unit (pure `detect`, synthetic `DataResource` fixtures):**

- one positive test per signal (`shared_accession`, `shared_identifier`, `explicit_link`,
  `version_lineage`), asserting the hint `kind`, the `resources` set, and the `key`.
- collapse: three resources sharing one accession → exactly one hint with three ids.
- negatives: two resources sharing only `organism="human"` → **no** hint; a resource's
  own doi appearing in its own `identifiers` → no self-hint.

**Handler (router.relate):**

- fail-soft: one id 404s on resolve → `errors` carries it, the other ids are still
  compared and their hint is returned.
- `< 2` resolved → empty hints + `note`, no raise.
- count guards: 1 id and 11 ids → `ValidationError`.

**Real-execution check (required by the project's real-execution rule):**

- one live `relate` over real ids that genuinely share structure — e.g. a GEO series and
  its linked SRA study (shared accession / explicit link), or a Zenodo dataset and its
  `described_in` paper (explicit link / shared identifier) — run against the live resolve
  path and asserted to produce the expected hint `kind`. Discover/verify the real ids live
  during implementation (no fabricated ids — same discipline as the recall-eval anchors).

## Non-goals (explicitly deferred)

- **Column/schema-level join hints** (compare tabular column names/types, detect
  Ensembl↔Entrez ID-class mismatches). Needs per-file range-reads + an ID-class detector
  - (for the convert suggestion) a gene-ID converter that does not exist yet. A clean
    follow-on once metadata-level `relate` is shipped and validated.
- **Executing** any join/merge/conversion — permanently ceded (HINTS-only boundary).
- **Grouped-cluster output** — output is a flat hint list (each hint names its resources);
  clusters are derivable downstream if ever needed. (Considered and declined for simplicity.)
- **Auto-relating search results** (a `search(relate=true)` flag) — declined: couples to
  the pagination window and adds cost to every opted-in search.

## Success criteria

- `relate(ids)` returns precise, evidence-backed hints for the four signals, with zero
  false hints on the negative fixtures.
- Pure `detect` is deterministic and covered by unit tests; the handler is fail-soft.
- One real-execution check passes against live ids.
- No new dependencies; single-responsibility modules; `relate.py` stays a pure function.
