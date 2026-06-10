# B3 — License-compatibility preflight (`use=` intent checker)

**Status:** PLANNED 2026-06-10. Target v0.30.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `use=<intent>` resolve enricher returning an ALLOW / REVIEW / DENY verdict for an intended use of a
resolved record, naming the governing licence clause and the normalized SPDX id. PURE function over a BUNDLED licence
matrix (no I/O — like `fair`, unlike `trust`). From the v4 memo [[data-aggregator-competitive-analysis-v4-2026-06-10]]
(B3, Frontier #1). Differentiator: cross-source licence reconciliation is structurally impossible for single-source
tools — we hold a normalized `license` field across 12 sources and can apply ONE consistent compatibility rule.

## Grounding (web-verified 2026-06-10)

- **choosealicense.com** (github/choosealicense.com, vendored into Licensee → powers GitHub's Licenses API) is the
  canonical machine-readable licence-property catalogue. Each licence carries `spdx-id` plus three flag sets:
  - `permissions`: `commercial-use`, `modifications`, `distribution`, `private-use`, `patent-use`
  - `conditions`: `include-copyright`, `document-changes`, `disclose-source`, `network-use-disclose`, `same-license`
  - `limitations`: `liability`, `warranty`, `trademark-use`, `patent-use`
- SPDX licence ids are the canonical short identifiers (spdx.org/licenses). We key the matrix on SPDX id.
- This wave bundles a CURATED SUBSET covering the licences actually seen on research data (CC family incl. NC/ND, CC0,
  MIT, Apache-2.0, BSD-2/3, GPL/LGPL/AGPL, MPL-2.0, ODbL-1.0, ODC-By-1.0, PDDL-1.0, Unlicense, plus the
  "all-rights-reserved / proprietary / no-licence" sentinel). HONEST coverage: an unrecognized or absent licence →
  **REVIEW**, never a fabricated ALLOW/DENY.
- **Not legal advice.** Every verdict carries a disclaimer: it is a metadata-derived compatibility _advisory_ computed
  from the stated licence, not a legal determination. EU-AI-Act / licence-crisis motivated, but framed as triage.

## Design — pure, opt-in, parallels `fair` (bundled matrix, no network)

- New module `license_compat.py`:
  - `normalize_spdx(license_str: str | None) -> str | None` — map a free/varied licence string to a canonical SPDX id
    (or `None` if unrecognized). Handles: bare SPDX ids (`CC-BY-4.0`, `MIT`), spaced/cased prose (`CC BY 4.0`,
    `Apache License 2.0`), and CC/OSI **URLs** (`https://creativecommons.org/licenses/by-nc/4.0/` → `CC-BY-NC-4.0`,
    `creativecommons.org/publicdomain/zero/1.0` → `CC0-1.0`). Generalizes (and supersedes the duplication of) the
    `fair._machine_readable_license` recognizer intent — but `fair` keeps its own boolean (no cross-module coupling at
    this stage; a later refactor can route `fair` through `normalize_spdx`).
  - `LICENSE_MATRIX: dict[str, LicenseProfile]` — SPDX-id → frozen permissions/conditions/limitations sets, sourced
    verbatim from choosealicense.com flag names. Document the source + fetch date in the module docstring.
  - `INTENTS: dict[str, tuple[str, ...]]` — intended-use → required `permissions` flags:
    - `commercial` → `("commercial-use",)`
    - `redistribute` → `("distribution",)`
    - `modify` → `("modifications",)`
    - `ml-training` → `("commercial-use", "modifications")` (training a model = a derivative use, usually commercial;
      ND/NC licences therefore DENY). Document this rule honestly in the docstring — it is OUR interpretation, stated.
  - `check(license_str: str | None, use: str) -> LicenseVerdict` — verdict logic:
    - licence unrecognized OR absent → `REVIEW`, reason names "licence not stated / not recognized; defaults to
      all-rights-reserved", `spdx_id=None`.
    - unknown `use` value → raise `ValueError` (fail loud — it is a caller error, mirrors router's strict inputs).
    - all required permissions present in the matched profile → `ALLOW`.
    - any required permission absent → `DENY`, `clause` names the missing permission(s) (e.g. "commercial-use not
      granted (NonCommercial)").
    - **`same-license` / `disclose-source` conditions** present on an otherwise-ALLOW licence → downgrade to `REVIEW`
      with the condition named (copyleft obligations the user must honour) — for `redistribute`/`ml-training` intents
      only; a bare `commercial` check on MIT stays ALLOW. (Keep this rule small and explicit; document it.)
- `models.LicenseVerdict(BaseModel)`: `use: str`, `verdict: Literal["ALLOW","REVIEW","DENY"]`, `spdx_id: str | None`,
  `license_raw: str | None` (the input string), `reason: str` (human-readable, names the clause), `disclaimer: str`
  (constant advisory disclaimer). Add `DataResource.license_compat: LicenseVerdict | None = None` (after `fair`).
- Wiring (mirror `fair`): `server.py` resolve handler — `if (u := args.get("use")): resource =
resource.model_copy(update={"license_compat": license_compat.check(resource.license, u)})`. Add a `use` STRING input
  (enum-documented: commercial/redistribute/modify/ml-training) to the resolve inputSchema. `model_dump()` serializes it.

## Tests (tests/test_license_compat.py)

- `normalize_spdx`: bare ids, spaced/cased prose, CC + CC0 URLs → canonical SPDX; junk/None → None. Table-driven.
- Matrix integrity: every value in `INTENTS` references a real permission flag; every profile's flags are drawn from
  the documented choosealicense vocab (no typos / invented flags). Assert a couple of anchor profiles literally
  (CC-BY-NC-4.0 lacks `commercial-use`; MIT has `commercial-use`+`modifications`+`distribution`; CC0 has all; GPL-3.0
  has `same-license` condition).
- `check` verdict matrix — one assertion per (licence, use) cell that matters:
  - MIT + commercial → ALLOW; MIT + ml-training → ALLOW.
  - CC-BY-NC-4.0 + commercial → DENY (clause names NonCommercial / commercial-use); + redistribute → ALLOW.
  - CC-BY-ND-4.0 + modify → DENY; + ml-training → DENY (no modifications).
  - GPL-3.0 + redistribute → REVIEW (same-license/copyleft named); GPL-3.0 + commercial → ALLOW.
  - CC0-1.0 + every intent → ALLOW.
  - "all rights reserved" / None licence + any intent → REVIEW (not DENY — honest, name "no licence stated").
  - unrecognized "see the paper" → REVIEW with spdx_id None.
  - unknown `use="teleport"` → raises ValueError.
- Verdict always carries the non-empty `disclaimer`; `spdx_id` is None exactly when the licence was unrecognized/absent.
- Determinism/purity: `check` does NO I/O (no httpx import path touched); calling twice gives identical results;
  signature takes only `(license_str, use)` — no client arg.
- server: `use` in resolve inputSchema; handler attaches `license_compat`; `model_dump()` carries it; absent `use` → None;
  an unknown `use` surfaces as a loud error (not a silent None).
- **Real-execution check:** run `check` on ≥2 REAL resolved records (gated live, `DATA_AGGREGATOR_MCP_LIVE=1`) — e.g. a
  CC-BY Zenodo DOI (→ commercial ALLOW) and a record whose source reports an NC or absent licence (→ DENY/REVIEW) —
  asserting the verdicts are sane and the `spdx_id` normalization matched the real licence string the source returned.
  Catches real-world licence-string shapes a synthetic fixture hides.

## Explicitly OUT of scope (deferred follow-ups — keep the wave tight)

- **Croissant `usageInfo` → full `odrl:Offer`.** B2 deliberately emits only a licence pointer and pins
  `test_no_odrl_permission_keys_in_b2_output`. Emitting a structured ODRL policy means _reversing_ that test and
  fabricating permission/prohibition triples — higher risk, and it belongs with B10's dossier where the matrix is
  consumed for provenance. The `LICENSE_MATRIX` built here is the reusable backend for it. Note in CHANGELOG as deferred.
- **Search-time `use=` filter across a whole result set** (advisory column on every hit) — resolve-only for v1, parity
  with trust/fair. A `strict_license` enforced-fetch gate is B12 (07-28, elicitation).

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.30.0** (pyproject/**init**/server.json×2/
  test_packaging) + CHANGELOG. Release after the wave is green (standing program authorization). ff-merge → tag v0.30.0.

## Spec contracts reviewers must enforce

- **No fabricated verdict** — unrecognized/absent licence is REVIEW with `spdx_id=None`, never a guessed ALLOW/DENY.
- **DENY names the missing permission / governing clause**; REVIEW names why (copyleft condition, or no licence stated).
- **`check` is PURE** (no network/file I/O); deterministic; unknown `use` fails LOUD (ValueError).
- **Matrix flags use the documented choosealicense vocab verbatim** — no invented flag names.
- **Every verdict carries the not-legal-advice disclaimer.**
- Croissant ODRL upgrade stays OUT (B2's no-ODRL test must remain green).
