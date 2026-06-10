# B10a — Per-record provenance "data-availability dossier" (RO-Crate export)

**Status:** PLANNED 2026-06-10. Target v0.33.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `resolve(format=provenance)` that renders a single **RO-Crate 1.1 data-availability dossier** for one
resolved record — a machine-readable artifact COMPOSING every provenance/integrity signal the server already computes:
version-currency (B1), licence + normalized SPDX (B3), FAIRness (B4), retraction/expression-of-concern (trust), and the
source/DOI/ID-chain. From the v4 memo [[data-aggregator-competitive-analysis-v4-2026-06-10]] (B10, Frontier #10). The
"why an aggregator" artifact for the 2026 AI-Act data-provenance moment: we are the single point that holds all of these
at once. **User chose: per-record dossier FIRST, then the whole-search Run Crate (B10b).**

## Grounding (web-verified 2026-06-10)

- RO-Crate 1.1: `conformsTo` is a versioned permalink under `https://w3id.org/ro/crate/` → use
  `https://w3id.org/ro/crate/1.1` (the value `ro_crate.py` already uses). Sources: researchobject.org/ro-crate/
  specification/1.1/ (metadata, provenance, root-data-entity pages).
- **Data-entity provenance pattern:** to record an action that produced/assessed an entity, attach a schema.org
  `CreateAction` (or `UpdateAction`) to the entity (or, for the crate itself, to the **root data entity**) — RO-Crate
  1.1 "Provenance of entities". The CreateAction's `instrument` points at the software agent that performed it.
- **NOT a "Provenance Run Crate".** That term is the Workflow-Run-Crate profile for _workflow executions_ — wrong shape
  for a per-record data-availability dossier. B10a is plain RO-Crate 1.1 + a CreateAction assessment entity. (B10b may
  revisit a run-style crate for a whole search.)

## Design — a new PURE renderer, composed by the resolve handler

- New module `dossier.py`: `render(resource: DataResource) -> dict[str, Any]` — PURE, no I/O. Builds the RO-Crate by
  REUSING `ro_crate.render(resource)` as the base graph (metadata descriptor + root `Dataset` + file entities), then
  EXTENDING `@graph` with the provenance/assessment entities below. (Reuse, don't re-derive, so the base crate can't
  drift from `ro_crate`.)
- The dossier adds ONE `CreateAction` provenance entity + assessment result entities linked from it:
  - `{"@id": "#provenance-assessment", "@type": "CreateAction", "name": "data-aggregator-mcp provenance assessment",
"instrument": {"@id": "https://github.com/musharna/data-aggregator-mcp"}, "object": {"@id": "./"},
"result": [ ...assessment entities... ]}` plus an `endTime` ONLY if `resource.last_updated` is set.
  - The software agent entity `{"@id": "https://github.com/musharna/data-aggregator-mcp", "@type": "SoftwareApplication",
"name": "data-aggregator-mcp", "version": "<__version__>"}`.
  - Assessment results as schema.org `PropertyValue` entities (only those whose signal is PRESENT — see honesty):
    - **version-currency:** `is_latest` / `superseded_by` (when `is_latest is not None`).
    - **licence:** raw `resource.license` + `normalized_spdx` (via `license_compat.normalize_spdx`, pure) when a licence
      is present; when SPDX normalization is None, say "unrecognized", never invent one.
    - **FAIR:** score + the four sub-scores + `assessed` + gaps, from `resource.fair` (attached by the handler).
    - **retraction:** from `resource.trust` — `retracted` / `retraction_doi` / `concern`. **Honesty (mirror
      TrustSignals):** `None` ⇒ emit "unknown / not checked", NEVER "not retracted". A definitive `False` may say
      "no retraction on record (Crossref)".
    - **source/identifier provenance:** the source repo, canonical id, DOI, cross-identifiers, accessions, and qualified
      `links` (rel → target) — the ID chain that shows where the record came from and what it relates to.
- Root data entity in the dossier also carries `conformsTo` for any export profiles already present is NOT required;
  keep `conformsTo` only on the metadata descriptor (RO-Crate 1.1). Do not fabricate a custom profile URI.

## Resolve-handler wiring (server.py) — compose, then render

- Extend the resolve `format` enum to `["croissant", "ro-crate", "provenance"]`; document `provenance` in the schema +
  the resolve description: "a one-call RO-Crate 1.1 data-availability dossier bundling version-currency, licence+SPDX,
  FAIR score, retraction status, and the source/DOI/ID chain (attached under `provenance`)".
- Add `DataResource.provenance: dict[str, Any] | None = None` (after `ro_crate`), inline comment noting the export.
- In the `case "resolve"` handler, AFTER the existing `cite`/`format=croissant|ro-crate`/`trust`/`fair`/`use` blocks,
  add (so it can REUSE already-attached enrichers and only compute what's missing):
  ```python
  if fmt == "provenance":
      if resource.fair is None:
          resource = resource.model_copy(update={"fair": fair_mod.assess(resource)})
      if resource.trust is None:
          resource = resource.model_copy(update={"trust": await trust_mod.annotate(client, resource)})
      resource = resource.model_copy(update={"provenance": dossier_mod.render(resource)})
  ```
  (`format=provenance` therefore IMPLIES the FAIR + retraction enrichment so the dossier is complete in one call —
  document this. It does not set the `croissant`/`ro_crate` fields; those stay opt-in via their own format values.)
- `import` the new module as `dossier as dossier_mod` next to the other enricher imports.

## Tests (tests/test_dossier.py + handler wiring)

- `dossier.render` is PURE (no httpx import path; deterministic — calling twice gives identical dicts; signature takes
  only `resource`).
- Structural validity: result has `@context`, `@graph`; `conformsTo` is `https://w3id.org/ro/crate/1.1`; the root
  `./ Dataset` entity is present; the `#provenance-assessment` `CreateAction` is present with `instrument` →
  the SoftwareApplication agent (carrying `__version__`), `object` → `{"@id": "./"}`, and a `result` list.
- Composition (each signal becomes a PropertyValue/entity ONLY when present):
  - a record with `is_latest=False, superseded_by="zenodo:9"` → a version-currency result names the superseding id;
    a record with `is_latest=None` → NO version-currency result (don't fabricate).
  - licence present (`cc-by-4.0`) → a licence result with `normalized_spdx == "CC-BY-4.0"`; a free-text licence
    (`see paper`) → result present with SPDX "unrecognized"/None, never invented; no licence → no licence result.
  - `resource.fair` attached → a FAIR result carrying score + sub-scores + gaps; absent fair → no FAIR result.
  - **retraction honesty:** `trust.retracted=None` → the dossier says unknown/not-checked, asserts NOTHING negative
    (assert the string does NOT claim "not retracted"); `trust.retracted=True` with a `retraction_doi` → names it;
    `trust.retracted=False` → may state "no retraction on record".
  - source/DOI/ID-chain: the source, canonical id, DOI, and a `links` rel/target appear in the dossier.
- Handler wiring: `format` enum includes `provenance`; `resolve(format=provenance)` attaches a non-None `provenance`
  dict; it AUTO-attaches `fair` and `trust` (assert both non-None in the dumped result); passing `fair=true`/`trust=true`
  alongside does not double-compute incorrectly (idempotent — reuse). `model_dump()` carries `provenance`.
- `DataResource.provenance` defaults to None.
- **Real-execution check (gated `DATA_AGGREGATOR_MCP_LIVE=1`):** resolve a REAL record end-to-end with
  `format=provenance` (e.g. a Zenodo DOI), assert the dossier is well-formed RO-Crate 1.1, that the FAIR + version +
  licence results reflect the real metadata, and that the retraction result makes NO negative claim when trust is
  unknown. Catches a real-metadata shape a synthetic fixture hides.

## Explicitly OUT of scope (→ B10b, the next wave)

- **Whole-search Run Crate** — one call documenting every source queried + per-hit provenance for an entire result set.
  That is task #25 (B10b), the user's chosen follow-up. B10a stays per-record so each wave is tight + reviewable.
- A Croissant-flavored dossier (RO-Crate is the right carrier for a data-availability statement; Croissant already
  carries its own provenance block from B2). Note as a possible future, not built.

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.33.0** (pyproject/**init**/server.json×2/
  test_packaging) + CHANGELOG (house style — document the RO-Crate 1.1 dossier, the composed signals, the
  FAIR+retraction auto-enrichment, the unknown-never-negative honesty, and that B10b whole-search is the follow-up).
  Release after green. ff-merge → tag v0.33.0.

## Spec contracts reviewers must enforce

- **Honesty / no fabrication** — only signals actually present are represented; an unknown retraction is "unknown",
  NEVER "not retracted"; an unrecognized licence is not assigned a fake SPDX; a missing FAIR/version signal is omitted.
- **Valid RO-Crate 1.1** — `conformsTo https://w3id.org/ro/crate/1.1`; the provenance entity is a real schema.org
  `CreateAction` with `instrument`/`object`/`result`; no invented profile URI.
- **Reuse `ro_crate.render`** as the base (no divergent re-derivation of the base crate).
- **PURE `render`** — deterministic, no network/file I/O; the handler (not the renderer) does the FAIR/trust enrichment.
- **One-call completeness** — `format=provenance` auto-attaches FAIR + retraction so the dossier is whole; reusing
  already-attached enrichers (idempotent), not double-computing.
