# B4 — FAIR-score per resource (RDA-Maturity-Model-grounded, pure-function enricher)

**Status:** PLANNED 2026-06-10. Target v0.29.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `fair=` resolve enricher computing a 0–100 FAIRness score + F/A/I/R sub-scores + actionable gaps,
as PURE functions over the normalized `DataResource` (no I/O). Grounded in the **RDA FAIR Data Maturity Model** (2020,
RD-Alliance) machine-evaluable indicators so the score is defensible, not arbitrary. From the v4 memo
[[data-aggregator-competitive-analysis-v4-2026-06-10]] (B4). Differentiator: we score CONSISTENTLY across 12 sources;
a per-repo FAIR evaluator can't.

## Grounding (web-verified 2026-06-10)

- RDA FAIR Data Maturity Model — Specification & Guidelines v0.90 (rd-alliance.org). Indicators are ID'd
  (`RDA-F1-01D`, `RDA-F2-01M`, `RDA-A1-02M`, `RDA-R1.1-01M`/`-03M`, …) with priorities (Essential/Important/Useful).
  `RDA-R1.1-03M` = "metadata refers to a machine-understandable reuse licence" (machine, not human-readable text).
- This wave implements a MACHINE-EVALUABLE SUBSET — only indicators computable from the metadata we already hold.
  Indicators requiring fetching the data or external probes are OUT (don't fabricate a pass/fail for what we can't see).

## Design — pure, opt-in, parallels `trust` (but no network)

- New module `fair.py`. `assess(resource) -> FairAssessment` is a PURE function (unlike `trust.annotate` which calls
  Crossref — FAIR is computed from the record, so no client/async needed). An INDICATOR TABLE drives it: each entry =
  `(dim, rda_id, weight, predicate(r)->bool, gap_message)`.
- `models.FairAssessment(BaseModel)`: `score: int` (0–100 overall), `findable/accessible/interoperable/reusable: int`
  (0–100 per dim), `assessed: int` (how many indicators were evaluated — transparency), `gaps: list[str]`
  (human-readable failed-indicator reasons, each naming its RDA indicator id). Add `DataResource.fair:
FairAssessment | None = None` (after `trust`).
- Scoring: per-dim score = `round(100 * sum(weight for passed) / sum(weight for all in dim))`; overall =
  `round(mean(the 4 dim scores))`. Weights: Essential=3, Important=2, Useful=1.
- Wiring (mirror `trust`): `server.py` resolve handler — `if args.get("fair"): resource =
resource.model_copy(update={"fair": fair_mod.assess(resource)})`. Add `fair` boolean to the resolve inputSchema.
  `result.model_dump()` serializes it. (Search-time FAIR is a deferred follow-up — resolve-only for v1, parity with trust.)

## Indicator table (the spec — machine-evaluable subset; impl cites these RDA ids in gaps)

FINDABLE

- F1 [Essential] persistent unique id — `bool(r.doi)` (DOI = globally-unique+persistent). gap: "no DOI/persistent
  identifier (RDA-F1-01D)". (Accession-only records fail F1 but may pass via the F-dimension's other indicators.)
- F2 [Essential] rich metadata — `bool(r.title) and bool(r.description) and bool(r.creators or r.subjects)`.
  gap: "sparse metadata: needs description + creators/subjects (RDA-F2-01M)".
- F3 [Important] metadata includes the data identifier — `bool(r.doi or r.accessions or r.identifiers)`.
  gap: "metadata exposes no resolvable data identifier (RDA-F3-01M)".
- F4 [Important] indexed in a searchable resource — always True (the record came from a searchable registry/our
  fan-out). No gap. (Document this as a justified constant, not a freebie.)
  ACCESSIBLE
- A1 [Essential] retrievable by id over a standard protocol — `bool(r.doi) or any(f.url for f in r.files)`.
  gap: "no resolvable identifier or download URL (RDA-A1-01M)".
- A1.1 [Important] open/free protocol — `any((f.url or '').startswith('https://') for f in r.files) or bool(r.doi)`.
  gap: "no open (https/doi) access protocol (RDA-A1.1-01M)".
- A2 [Important] metadata persists independently of data — True for registry-backed sources (DataCite/registries
  keep metadata after data withdrawal). Treat as True; if `r.access == 'closed'` keep A2 True (metadata still there).
  INTEROPERABLE
- I1 [Essential] formal/known representation — `any(f.mime for f in r.files)`.
  gap: "no machine-readable file formats declared (RDA-I1-01M)".
- I2 [Useful] FAIR vocabularies — `bool(r.taxa) or bool(r.subjects)` (NCBI taxa = a FAIR vocab; subjects = controlled
  terms). gap: "no controlled-vocabulary terms (taxa/subjects) (RDA-I2-01M)".
- I3 [Important] qualified references to other (meta)data — `bool(r.links)`.
  gap: "no qualified links to related records (RDA-I3-01M)".
  REUSABLE
- R1.1a [Essential] license present — `bool(r.license)`. gap: "no reuse licence (RDA-R1.1-01M)".
- R1.1b [Important] machine-understandable licence — license matches a known SPDX/CC id (small recognizer:
  startswith/contains cc-/cc0/by/mit/apache/gpl/bsd/odbl/public domain, or a SPDX-shaped token; NOT free prose).
  gap: "licence is free text, not a machine-readable id (RDA-R1.1-03M)".
- R1.2 [Important] provenance — `bool(r.creators) and bool(r.funding or r.last_updated or r.links or r.source)`.
  gap: "thin provenance: needs creators + (funding/dateModified/relations) (RDA-R1.2-01M)".
- R1.3 [Useful] community standards — `any((f.name or '').lower().endswith(known_exts) for f in r.files) or
bool(r.accessions)` (known scientific formats or a domain accession). gap: "no recognised community-standard format
  (RDA-R1.3-01M)".

(The impl may refine predicates but MUST keep them honest — never score an indicator we can't evaluate, and frame
gaps as "metadata does not expose X", not "the dataset is bad".)

## Tests (tests/test_fair.py)

- A fully-populated resource → high score (>80), all four sub-scores high, gaps minimal.
- A bare resource (id+title only) → low score, gaps list every failed Essential/Important indicator with its RDA id.
- Each indicator: a targeted on/off pair proving the predicate flips the right sub-score and adds/removes its gap.
- R1.1b machine-readable-licence recognizer: "cc-by-4.0"/"MIT"/"CC0-1.0" → pass; "see LICENSE.txt"/"Contact authors"
  → fail (gap present) WHILE R1.1a (license present) passes — the two are distinct.
- `assessed` count is stable and equals the number of indicators evaluated.
- Score math: a constructed resource passing a known weighted subset → assert the exact computed sub-score and overall
  (pin the formula).
- Determinism/purity: `assess` does NO I/O (no httpx); calling twice gives identical results.
- server: `fair` in resolve inputSchema; handler attaches `fair`; `model_dump()` carries it; `fair=false`/absent → None.
- **Real-execution check:** run `assess` on ≥2 REAL resolved records (gated live, `DATA_AGGREGATOR_MCP_LIVE=1`) — e.g.
  a Zenodo DOI (rich) and a GEO accession (sparser) — and assert the scores are sane + ordered (rich > sparse) and the
  gaps are plausible. Catches metadata-shape assumptions a synthetic fixture hides.

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.29.0** (pyproject/**init**/server.json×2/
  test_packaging) + CHANGELOG. Release after the wave is green (standing program authorization). ff-merge → tag v0.29.0.

## Spec contracts reviewers must enforce

- **No fabricated indicators** — only score what's evaluable from the record; `assessed` must equal the real count.
- **R1.1a (license present) and R1.1b (machine-readable) are DISTINCT** — a free-text license passes a, fails b.
- **Gaps name their RDA indicator id** and are framed as metadata-exposure gaps, not value judgements.
- **`assess` is PURE** (no network/file I/O); deterministic.
- Score math exactly as specified (weights Essential=3/Important=2/Useful=1; per-dim ratio; overall = mean of 4).
