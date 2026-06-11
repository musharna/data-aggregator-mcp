# A2.P1 — `search(understand=true)`: LLM NL→structured-query rewriting

**Status:** PLANNED 2026-06-10. Target v0.36.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `search(understand=true)` that, when an LLM endpoint is configured, rewrites a free-text query into
a **keyword core + structured parameters** (organism/disease/tissue/chemical/assay + kind/year filters), then runs the
EXISTING fan-out. The extracted ontology entities flow through the ALREADY-SHIPPED ontology resolvers
(`_expand_organism`/`_disease`/`_tissue`/`_chemical`/`_assay`) — so **the LLM proposes, the deterministic
NCBI/MeSH/UBERON/ChEBI/EDAM resolvers dispose**: a hallucinated entity that doesn't resolve simply yields no expansion.
This is A2 phase 1 (query-understanding) from the scoping memo [[da-a2-scoping-2026-06-10]]; it raises RECALL on the
QUERY side (the realistic shape for a stateless, no-owned-corpus aggregator — corpus vector index is CEDED to EOSC).
From the v4 memo [[data-aggregator-competitive-analysis-v4-2026-06-10]] (A2). **User chose: opt-in LLM endpoint + P1.**

## Architecture stance (the honesty + safety spine)

- **Opt-in, fail-soft, zero new required deps.** Mirror the EXISTING `embeddings.py` pattern EXACTLY: a remote
  OpenAI-compatible endpoint enabled only by env (`LLM_API_BASE`); absent or failing → the search behaves byte-identically
  to today (raw keyword query), with a transparency note in `errors["understand"]`. The LLM call NEVER raises into the
  search path.
- **The ontology resolvers are the hallucination guardrail.** The rewriter only PROPOSES `organism="Zea mays"` etc.; the
  shipped `_expand_*` resolvers validate against the real ontology and fail-loud/echo exactly as they do for a
  user-supplied param. No new trust surface — an LLM-invented organism that NCBI Taxonomy doesn't know yields no
  expansion, same as a user typo.
- **Explicit caller params always win.** If the caller passed `organism=`/`disease=`/`kind=`/etc. explicitly, the
  rewriter's value for that field is IGNORED (the human was specific on purpose). The rewriter only FILLS fields the
  caller left None.
- **Transparent echo.** A new `SearchResult.query_understanding` echoes the raw query, the LLM's structured
  interpretation, and exactly which fields were APPLIED (vs. overridden-by-explicit vs. dropped-as-unresolved) —
  mirroring the `*_expansion` echo honesty. The user can always see what the LLM did to their query.
- **Determinism for tests.** The LLM is the only nondeterministic part; it is fully mockable (like `embeddings.embed`).
  All plumbing is deterministic and unit-tested with a mocked rewrite.

## Grounding (reuses shipped code — verified live this session)

- `router.search_page(client, *, query, size, sources, organism, disease, tissue, chemical, assay, published_after,
published_before, kind, cursor, rank, collapse_mirrors)` (`router.py:497`). On a FRESH search (the `else` branch,
  ~L549) it builds `filters = {published_after, published_before, kind}` then runs the ontology chain
  `effective_query, expansion = await _expand_organism(client, query, organism, errors)` → `_expand_disease` →
  `_tissue` → `_chemical` → `_assay` (each takes the running `effective_query` + the param, returns the expanded query +
  an echo or None, appends to `errors` on lookup failure). On a CONTINUATION (`cursor`, ~L528) EVERY param is read from
  the cursor and nothing re-expands. The `SearchResult` is built at the end (~L688) with the five `*_expansion` echoes.
- `embeddings.py` is the opt-in-remote-endpoint template: `_config()` reads `EMBEDDING_API_BASE`/`_KEY`/`_MODEL`,
  returns None if unset; `embed()` POSTs via `_http.request_json(client, "POST", url, service=..., content=payload,
headers=...)` and returns None on ANY exception (fail-soft). Mirror this for `llm.py`.
- `_http.request_json(client, method, url, *, service, params, data, content, headers, timeout=30, max_retries=3,
not_found_returns)` returns parsed JSON. Use it for the chat call.
- `models.py`: `SearchResult` (fields query/total/count/results/errors/next_cursor + five `*_expansion` +
  `provenance_crate`). Add `query_understanding` after the expansions. The `*Expansion` models are the echo-shape
  precedent.
- `server.py`: the `search` Tool inputSchema (~L342) + the `case "search":` handler (~L754) that threads args into
  `search_page(...)`. `_cursor` encode/decode (grep `_cursor.encode`/`decode`) carries query/organism/disease/tissue/
  chemical/assay/kind(via filters)/etc. — the POST-rewrite values must be what gets encoded (see Pagination below).

## New module: `llm.py` — opt-in OpenAI-compatible chat, fail-soft

- `_config() -> tuple[str, str | None, str] | None`: reads `LLM_API_BASE` (required-to-enable), `LLM_API_KEY`
  (optional — keyless local servers supported), `LLM_MODEL` (default a sensible small instruct model name, e.g.
  `"gpt-4o-mini"`; it is just a string passed through — document that the user sets it to whatever their endpoint
  serves). Returns None when `LLM_API_BASE` unset.
- `async def complete_json(client, *, system: str, user: str) -> dict[str, Any] | None`: POSTs to
  `{base}/chat/completions` with `{"model": model, "messages": [{role:system},{role:user}], "temperature": 0,
"response_format": {"type": "json_object"}}` (temperature 0 for stability; `response_format` json_object — document
  that endpoints which ignore it still work because we parse defensively). Auth header only when key present. Parse the
  assistant message content as JSON; return the dict. Return None on ANY exception OR non-dict/parse failure — NEVER
  raise. (Same `try/except Exception → None` discipline as `embeddings.embed`.)
- PURE-of-side-effects beyond the one HTTP call; no global state.

## New module: `query_understanding.py` — the rewriter + echo

- A parsed-rewrite dataclass/model (internal) with optional fields: `keyword_core: str | None`, `organism`, `disease`,
  `tissue`, `chemical`, `assay` (all `str | None`), `kind: str | None`, `year_min: int | None`, `year_max: int | None`.
- `async def rewrite(client, query: str) -> ParsedRewrite | None`:
  - `cfg`-gate via `llm._config()`; if None → return None (caller stays raw).
  - Build a TEMPLATE-CONSTRAINED prompt. System: "You convert a free-text research-data search query into a structured
    search. Return STRICT JSON with EXACTLY these keys: keyword_core (string: the core search terms with
    natural-language fluff removed), organism, disease, tissue, chemical, assay (each: a single canonical entity NAME or
    null — do NOT invent; null if not clearly present), kind (one of dataset|sequencing_run|study|publication|software
    or null), year_min, year_max (integers or null). Do not add keys. Do not explain." User: the raw query.
  - Call `llm.complete_json`. If None → return None. Validate defensively: keep only the known keys; coerce
    year_min/year_max to int-or-None; DROP `kind` if not in the valid set (do not pass an invalid kind downstream);
    empty-string → None. If parsing yields nothing usable (all None) → return None (no-op rewrite; nothing to echo).
  - Return the `ParsedRewrite`. (No network beyond the one LLM call; the ontology validation happens later via the
    resolvers — keep this module free of ontology calls.)
- `QueryUnderstanding` pydantic echo model (in `models.py`, see below) — built by the router after it knows what was
  applied.

## Models (models.py)

- `class QueryUnderstanding(BaseModel)`: echo of the LLM rewrite that fired (transparency, mirrors `*Expansion`):
  - `input: str` (the raw query as given),
  - `keyword_core: str | None` (the rewritten keyword query actually used, or None if the LLM didn't change it),
  - `extracted: dict[str, Any]` (the LLM's full structured interpretation — every non-null field it returned),
  - `applied: dict[str, Any]` (the subset actually APPLIED to this search — i.e. fields the caller left None and that
    survived validation; ontology entities here STILL had to resolve to expand, which the `*_expansion` echoes show),
  - `overridden: list[str]` (fields the LLM proposed but the caller had set explicitly → ignored).
    Document: "the LLM proposes; ontology resolvers validate (see the \*\_expansion echoes); explicit caller params win."
- `SearchResult.query_understanding: QueryUnderstanding | None = None` (after `assay_expansion`, before
  `provenance_crate`).

## Router wiring (router.py) — understand BEFORE expand, in the fresh-search branch only

- Add `understand: bool = False` to `search_page`'s signature.
- In the `else` (fresh-search) branch, BEFORE building `filters` and BEFORE the `_expand_*` chain:
  ```python
  query_understanding = None
  if understand:
      ru = await query_understanding_mod.rewrite(client, query)   # fail-soft → None
      if ru is None:
          errors["understand"] = "query understanding unavailable (no LLM endpoint configured or rewrite failed)"
      else:
          # explicit caller params win; the rewriter only fills None fields
          overridden = []
          if ru.keyword_core: query = ru.keyword_core
          for field, val in (("organism", ru.organism), ("disease", ru.disease), ("tissue", ru.tissue),
                             ("chemical", ru.chemical), ("assay", ru.assay)):
              if val is None: continue
              if locals()[field] is not None: overridden.append(field); continue   # caller was explicit
              <assign the local param = val>
          if ru.kind and kind is None and ru.kind in _VALID_KINDS: kind = ru.kind
          if ru.year_min is not None and published_after is None: published_after = ru.year_min
          if ru.year_max is not None and published_before is None: published_before = ru.year_max
          query_understanding = <build QueryUnderstanding echo: input=<original raw query>, keyword_core, extracted, applied, overridden>
  ```
  (Capture the ORIGINAL raw query for the echo `input` BEFORE reassigning `query`. Do NOT use `locals()` dynamic
  assignment in the real impl — write explicit per-field `if`s; the pseudocode above is illustrative. Keep it readable
  and mypy-clean.)
- The existing `filters`-build + `_expand_*` chain then runs on the POSSIBLY-REWRITTEN `query` + filled params,
  UNCHANGED. The extracted entities thus get validated/echoed by the real resolvers.
- Pass `query_understanding` into the `SearchResult(...)` constructor.
- **Continuation branch: do NOT re-understand** (frozen, like no-re-expand). Set `query_understanding = None` there.

## Pagination / cursor (must stay consistent)

- Understanding runs ONCE on the fresh search and MUTATES `query`/`organism`/`disease`/`tissue`/`chemical`/`assay`/
  `kind`/`published_after`/`published_before` BEFORE the cursor is encoded at the end — so the existing cursor-encode
  (which already serializes these) naturally captures the POST-rewrite values. Verify the encode happens after the
  mutation (it does — cursor is built near the end). On continuation those post-rewrite values are replayed and
  `understand` is not re-read. **Net: a paged understood-search stays consistent page-to-page with no new cursor field**
  — but ADD a regression test asserting page 2 of an `understand=true` search uses the rewritten query (decode the
  next_cursor and assert).

## Server wiring (server.py)

- Add `understand` (boolean, default false) to the `search` inputSchema, documented: "Opt into LLM query understanding:
  a free-text query is rewritten into a keyword core + structured params (organism/disease/tissue/chemical/assay, kind,
  year) before fan-out; extracted entities are validated by the same ontology resolvers (a hallucinated entity that
  doesn't resolve is simply dropped), explicit params you pass always win, and the interpretation is echoed in
  query_understanding. Requires an LLM endpoint (LLM_API_BASE); with none configured the search runs unchanged and notes
  it in errors['understand']."
- Thread `understand=args.get("understand", False)` into the `search_page(...)` call.
- Document the three env vars in the README/`.env.example` if one exists (grep; mirror how EMBEDDING_API_BASE is
  documented).

## Tests (tests/test_llm.py + tests/test_query_understanding.py + router/server wiring)

- **`llm.complete_json`:** disabled (no `LLM_API_BASE`) → None; configured + mocked 200 JSON-content response → parsed
  dict; malformed/exception → None (fail-soft, never raises); auth header present only when key set. (Mirror
  `tests/test_embeddings.py` structure — read it.)
- **`query_understanding.rewrite`:** mock `llm.complete_json` → a fixed dict; assert the `ParsedRewrite` maps fields,
  coerces years, DROPS an invalid `kind`, treats empty strings as None, and returns None when the LLM is unconfigured or
  returns nothing usable. NEVER raises.
- **Router plumbing (deterministic, mock `query_understanding.rewrite`):**
  - `understand=true` + a rewrite giving `keyword_core` + `organism` → the fan-out runs on the keyword_core and
    `organism` flows into `_expand_organism` (assert the taxon_expansion echo appears for a resolvable organism, e.g.
    monkeypatch the taxonomy resolver or use a known one); `query_understanding` echo is populated (input = original
    query, applied includes organism).
  - **explicit wins:** caller passes `organism="X"` AND the rewrite proposes `organism="Y"` → `X` is used, `organism`
    listed in `overridden`.
  - **hallucination guardrail:** a rewrite proposing an organism the resolver can't resolve → NO taxon_expansion (the
    resolver drops it), and the search still runs (no crash); the echo still shows it was extracted (honesty — the user
    sees what the LLM proposed even though it didn't resolve).
  - **fail-soft:** `understand=true` but `rewrite` returns None (no LLM) → search byte-identical to `understand=false`,
    `errors["understand"]` set, `query_understanding` is None.
  - `understand=false` (default) → byte-identical to today; `query_understanding` None; no LLM call attempted.
  - **pagination:** page-2 cursor of an `understand=true` search decodes to the REWRITTEN query (regression).
- **Server wiring:** `understand` in the inputSchema (boolean, not required); `search(understand=true)` threads through;
  `model_dump()` carries `query_understanding`; the FULL existing search suite passes with the flag off (purely additive).
- **Eval harness (recall-lift) — `scripts/eval_understand.py`, gated `DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE`:**
  a SMALL labeled set (~6–10 natural-language queries each with a few known-relevant DOIs/ids, committed as a JSON
  fixture) that runs each query with `understand=false` vs `understand=true`, computes recall@20 for each, and PRINTS
  the per-query + mean lift. NOT a hard pytest assertion (live recall varies) — it is the "show it works" instrument the
  scoping memo requires. Include a short README note on how to run it + interpret it. (If a real-execution unit anchor
  is wanted in-suite: one gated live test that runs a single NL query end-to-end with a real LLM endpoint and asserts the
  `query_understanding` echo is well-formed + the search returns results — mirror the existing gated live tests.)

## Explicitly OUT of scope (→ later A2 phases)

- **A2.P2 diverse multi-query expansion** (3–5 diverse queries fanned out + merged) — the next phase; bigger recall lift,
  N× fan-out cost, window-pagination. P1 produces ONE better structured query (no fan-out multiplication).
- **A2.P3 OpenAlex semantic federation** — separate source add.
- **Corpus vector index / HyDE / doc2query** — CEDED per the scoping memo (vector/owned-index techniques; EOSC's moat).
- **Re-ranking changes** — P1 leaves `rank=semantic` untouched; understanding and re-rank compose orthogonally.

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.36.0** (pyproject/**init**/server.json
  (BOTH version fields)/test_packaging) + CHANGELOG (house style — document `search(understand=true)`, the opt-in
  fail-soft LLM endpoint, the propose-validate-dispose guardrail via the ontology resolvers, explicit-wins, the
  transparent `query_understanding` echo, and that this is A2.P1 with P2 multi-query next). Release after green.
  ff-merge → tag v0.36.0 → `gh release create`.

## Spec contracts reviewers must enforce

- **Opt-in + fail-soft + additive** — no `LLM_API_BASE` ⇒ search byte-identical to today (+ an `errors["understand"]`
  note only when `understand=true`); the LLM call NEVER raises into the search path.
- **Propose-validate-dispose** — extracted entities go through the SHIPPED `_expand_*` resolvers; an unresolvable entity
  yields no expansion (no new trust surface, no fabricated taxonomy).
- **Explicit caller params win** — the rewriter only fills None fields; overrides are recorded in the echo.
- **Transparent echo** — `query_understanding` shows input, keyword_core, full extracted interpretation, applied subset,
  and overridden fields; nothing the LLM did is hidden.
- **Pagination consistent** — post-rewrite values are what the cursor encodes; continuation never re-understands.
- **Determinism** — only the LLM is nondeterministic and it is fully mockable; all plumbing is deterministically tested.
- **Eval shipped** — a runnable (gated) recall-lift harness + labeled fixture, not just unit mocks.
