# A2.P2 — `search(multi_query=true)`: diverse multi-query recall expansion

**Status:** PLANNED 2026-06-10. Target v0.37.0. Subagent-driven TDD (impl → spec review → quality review).
**Decision:** opt-in `search(multi_query=true)` that, when an LLM endpoint is configured, generates up to N **deliberately
diverse** reformulations of the query, fans EACH across the sources, merges + dedups the union, and re-ranks the union
against the ORIGINAL query — surfacing relevant records a single lexical query missed. This is A2 phase 2 (the biggest
query-side recall lever) from the scoping memo [[da-a2-scoping-2026-06-10]] §P2; it composes with A2.P1 `understand=`
(P1 structures ONE query; P2 fans out N variants). Corpus vector index stays CEDED. Evidence: DMQR-RAG +14.45% P@5,
**conditioned on diversity** (naive multi-rewrites collapse to near-identical → enforce diversity + dedup). From the v4
memo [[data-aggregator-competitive-analysis-v4-2026-06-10]] (A2). **User chose: continue into P2 after P1 shipped.**

## Design stance (additive safety + honesty)

- **Opt-in, fail-soft, single-query path BYTE-IDENTICAL.** `multi_query=false` (default) ⇒ `search_page` behaves
  EXACTLY as today (the existing fan-out/offset/cursor code path is untouched — assert via the full existing suite). No
  `LLM_API_BASE` or any LLM failure with `multi_query=true` ⇒ degrade to a normal single-query search + an
  `errors["multi_query"]` note. The LLM call NEVER raises into the search path (same discipline as `llm.complete_json`).
- **The original query is ALWAYS variant 0.** Multi-query can only ADD recall, never drop below the single-query
  baseline — variant 0 is the (post-`understand`, post-expansion) query the single path would have run.
- **Diversity enforced.** The LLM prompt demands genuinely diverse reformulations (different facets/synonyms/framings,
  not paraphrases); we dedup case-insensitively and cap at `MAX_QUERY_VARIANTS` (4, incl. variant 0). Embedding-distance
  dedup is DEFERRED (note it).
- **Re-rank against the ORIGINAL query.** The union has no single coherent upstream order, so multi-query engages the
  window-rank consumption model (whole window consumed per page) and re-ranks against the user's original query via the
  shipped `embeddings.rerank`. No embedding endpoint ⇒ degrade to interleaved order + `errors["semantic"]` note — still
  a recall win (more candidates), just unranked. Honest.
- **Transparent echo.** A new `SearchResult.query_expansion` echoes the raw variants the LLM produced (so the user sees
  exactly which reformulations were fanned out). Per-variant ontology expansion is still shown by the `*_expansion`
  echoes (same params apply to every variant).
- **Cost disclosed + bounded.** Multi-query is N× the upstream fan-out (≤4 variants × M sources × `size`). Capped by
  `MAX_QUERY_VARIANTS`; documented in the tool description and CHANGELOG. (Per-source rate limits are the real cost,
  not compute.)

## Grounding (live this session — RE-READ before editing; Iron Law)

- `router.search_page` (`router.py:497`). Single-query fan-out (L658-688): `adapters = _select(sources)`;
  `names = list(adapters)`; `asyncio.gather(adapters[n].search(client, effective_query, size=size,
offset=offsets.get(n,0)) for n in names)`; builds `origin[id(r)]=name`, `per_source`, `totals[name]`, `total`;
  `merged = _dedup(interleave(per_source))`. Consumption (L690-721): `rank=="semantic"` re-ranks the whole window and
  consumes ALL of it (`cut=len-1`); else walk-to-cut. `new_offsets = {n: offsets.get(n,0)+consumed_per_adapter[n]}`
  (L723). `more`/`next_cursor` (L735-757) — cursor encodes `q/sources/organism/disease/tissue/chemical/assay/filters/
size/offsets/rank/collapse_mirrors`. Tail (L759-): `_enrich` → `_with_version_status` → optional `_collapse_mirrors`
  → `SearchResult`. Continuation branch (L531-549) reads every field from the cursor, freezes expansions, and (NOTE,
  existing behavior) sets `effective_query = query` (the stored `q`).
- Helpers reusable as-is: `_select`, `interleave`, `_dedup`, `embeddings.rerank`, `_passes_filters`, `_enrich`,
  `_with_version_status`, `_collapse_mirrors`, `_cursor.encode/decode`, `_expand_organism/_disease/_tissue/_chemical/
_assay`, `_VALID_KINDS`. A2.P1 added `query_understanding.rewrite` + `llm.complete_json` + the `understand` block
  (L558-637) which runs BEFORE the expansion chain and mutates `query`+params.
- `models.py`: `SearchResult` now carries `query_understanding` (A2.P1) before `provenance_crate`. The `*Expansion`
  models are the echo precedent.
- `server.py`: `search` inputSchema has `understand` (A2.P1); the handler threads it into `search_page`.

## New LLM function: `query_understanding.expand`

- `async def expand(client, query: str, *, n: int) -> list[str] | None`: gate on `llm._config()` (None → None). Build a
  diversity-demanding prompt (system: "Generate up to {n-1} ALTERNATIVE search queries that capture DIFFERENT facets,
  synonyms, and framings of the user's intent — genuinely diverse reformulations, NOT paraphrases. Return STRICT JSON
  {\"variants\": [string, ...]}. Omit the original. Do not explain."). Call `llm.complete_json`; parse `variants`
  (defensively: list of non-empty strings only); return None on failure/empty. The CALLER prepends the original query as
  variant 0, case-insensitively dedups, and caps to `MAX_QUERY_VARIANTS`. NEVER raises.
- Keep this in `query_understanding.py` next to `rewrite`. No ontology calls here.

## Models (models.py)

- `class QueryExpansion(BaseModel)`: `input: str` (original query), `variants: list[str]` (the RAW variants actually
  fanned out, incl. the original as the first entry). Docstring: transparency echo of A2.P2 multi-query recall
  expansion; each variant got the same ontology expansion (see `*_expansion`); results are the deduped union re-ranked
  against the original query.
- `SearchResult.query_expansion: QueryExpansion | None = None` (after `query_understanding`, before `provenance_crate`).

## Router: a parallel multi-query path (single-query path untouched)

Add `multi_query: bool = False` to `search_page`. Because the single-query offset/cursor model keys by `source` and must
stay byte-identical, multi-query gets a PARALLEL fan-out keyed by a composite `(variant_index, source)` label. Shared
TAIL logic (consume-window → emitted/consumed/cut; enrich+version+collapse → SearchResult) MAY be factored into small
helpers used by both paths, but the single-query observable behavior + cursor format MUST be byte-identical (tests
enforce).

**Fresh multi-query search** (only when `multi_query` AND not a cursor continuation):

1. Run the A2.P1 `understand` block first if `understand=true` (so variant 0 benefits from structuring). Then the raw
   base query for expansion is the (possibly-rewritten) `query`.
2. `variants_raw = await query_understanding.expand(client, query, n=MAX_QUERY_VARIANTS)`. If None →
   `errors["multi_query"] = "multi-query expansion unavailable (no LLM endpoint configured or expansion failed)"` and
   FALL BACK to the single-query path (variant 0 only) — i.e. set `multi_query` effectively off for this call.
3. Build the variant list: `variants = dedup_ci([query, *variants_raw])[:MAX_QUERY_VARIANTS]` (original always first).
4. For EACH variant, compute its `effective_variant` via the SAME ontology expansion chain
   (`_expand_organism`→…→`_expand_assay`) with the shared organism/disease/tissue/chemical/assay params. Capture the
   five `*_expansion` echoes ONCE (identical across variants — compute on variant 0). (Resolver lookups are cached →
   cheap to repeat.)
5. Composite fan-out: `streams = [((vi, name), adapters[name].search(client, eff_variant[vi], size=size,
offset=comp_offsets.get(key, 0))) for vi in range(len(variants)) for name in names]`. `asyncio.gather` them;
   `origin[id(r)] = (vi, name)`; `comp_totals[(vi,name)] = adapter_total`; `total = sum`.
6. `merged = _dedup(interleave(per_stream))` (interleave across the composite streams — reuse `interleave`/`_dedup`).
   `_dedup` already collapses the same record arriving from multiple variants (it keys on id/DOI — verify it dedups
   cross-variant duplicates, which is the whole point; add a test).
7. Consume via the WINDOW-RANK model (always, for multi-query): `embeddings.rerank(client, original_query, merged)` (the
   ORIGINAL pre-expansion user query is the anchor — capture it before `understand`/expansion mutates `query`); on
   `reason` set `errors["semantic"]`; emit top `size` passing filters; `consumed = merged`; `cut = len-1`.
8. `new_comp_offsets = {key: comp_offsets.get(key,0) + consumed_per_stream[key]}`. `more`/cursor as today but the cursor
   carries `variants` (the effective_variant strings — store EXPANDED so continuation needs no re-expand/no LLM) and
   composite offsets (serialize composite keys as `f"{vi}{name}"` for JSON; or a list of `[vi, name, offset]`).
9. Build `query_expansion = QueryExpansion(input=original_query, variants=variants)` (RAW variants for the echo).

**Multi-query continuation** (cursor carries `variants`): decode `variants` (already-expanded strings) + composite
offsets; re-fan each variant×source at its offset; NO LLM, NO re-expand (frozen); same window-rank consumption; re-emit
cursor. `query_expansion` echo MAY be rebuilt from the stored variants (or None on continuation — pick one and test it).

**Cursor disambiguation:** a multi-query cursor is identified by the presence of a `variants` key in the decoded state;
`search_page`'s continuation branch checks for it and routes to the multi path. Single-query cursors (no `variants`) are
byte-identical to today.

## Server wiring (server.py)

- Add `multi_query` (boolean, default false) to the `search` inputSchema, documented: "Opt into diverse multi-query
  recall expansion: an LLM generates up to a few deliberately-diverse reformulations of your query, each is fanned out
  across all sources, and the deduped union is re-ranked against your original query — surfacing relevant records a
  single keyword query would miss. Costs N× the upstream calls (bounded). Requires an LLM endpoint (LLM_API_BASE); with
  none configured the search runs as a normal single query and notes it in errors['multi_query']. The variants used are
  echoed in query_expansion. Composes with understand=." Thread `multi_query=args.get("multi_query", False)`.
- Document `MAX_QUERY_VARIANTS` behavior briefly in the README near the `understand`/LLM env section.

## Tests (new + extend; mirror existing router/cursor tests)

- **`query_understanding.expand`:** disabled→None; mocked variants list→parsed; garbage/empty→None; never raises.
- **Variant assembly:** original always first; case-insensitive dedup; capped at `MAX_QUERY_VARIANTS`.
- **Composite fan-out (mock `expand` + stub adapters):** N variants × M sources all queried; a record returned by TWO
  variants is DEDUPED to one in the output (cross-variant dedup — the core recall-without-duplication property);
  `total` reflects the composite sum; `query_expansion` echo lists the raw variants.
- **Re-rank anchoring:** the rerank is called with the ORIGINAL user query, not a rewritten/expanded variant (assert the
  anchor — monkeypatch `embeddings.rerank` to record its query arg); no embedding endpoint → interleaved order +
  `errors["semantic"]`, results still returned (recall win).
- **PAGINATION (the high-risk surface — test hard):** a multi-query search with `more=true` emits a cursor carrying
  `variants` + composite offsets; page 2 decodes it, re-fans the SAME variants at advanced offsets, does NOT call the
  LLM again (assert `expand` not awaited on continuation), and returns the NEXT window with no overlap/duplication vs
  page 1; the termination guard (`more=false` when the composite window is exhausted) holds; an empty composite window
  does not emit a replaying cursor.
- **Fail-soft + fallback:** `multi_query=true` with no LLM (expand→None) → behaves as a single-query search (variant 0),
  `errors["multi_query"]` set, `query_expansion` None; `multi_query=false` → BYTE-IDENTICAL to today (the full existing
  search + cursor suite passes untouched; single-query cursor format unchanged).
- **Compose with understand:** `understand=true` + `multi_query=true` → understand structures variant 0 (its
  keyword_core/params feed all variants); both `query_understanding` and `query_expansion` echoes are populated.
- **Server wiring:** `multi_query` in the inputSchema (boolean, not required); threads through; `model_dump()` carries
  `query_expansion`; full existing search suite passes with the flag off (additive).
- **Eval harness:** extend `scripts/eval_understand.py` (or add `scripts/eval_multi_query.py`) — gated
  `DATA_AGGREGATOR_MCP_LIVE=1` + `LLM_API_BASE` — to also report recall@20 for `multi_query=true` vs the single-query
  baseline over the labeled fixture, printing mean lift. The "show it works" instrument.

## Explicitly OUT of scope (deferred)

- **Embedding-distance variant diversity** (dedup near-paraphrases by vector distance) — v1 uses prompt-demanded
  diversity + case-insensitive dedup; note as future.
- **A2.P3 OpenAlex semantic federation** — separate source add.
- **Per-source variant budgeting beyond the global `MAX_QUERY_VARIANTS` cap** — note if rate limits bite.
- **Corpus vector index / HyDE / doc2query** — CEDED (scoping memo).

## Gates & release

- ruff + format + mypy (no ignores) + `pytest --cov-fail-under=92`. Bump **0.37.0** (pyproject/**init**/server.json
  BOTH fields/test*packaging `\_0360*`→`_0370_`) + CHANGELOG (house style — document `search(multi_query=true)`, the
diverse-variant LLM expansion, the composite-key window-paginated fan-out, the original-query re-rank anchor, the N×
cost cap, fail-soft fallback, byte-identical single-query, and that this is A2.P2 with P3 OpenAlex-semantic optional
next). Release after green. ff-merge → tag v0.37.0 → `gh release create`.

## Spec contracts reviewers must enforce

- **Single-query byte-identical** — `multi_query=false` and every existing cursor/test path is observably unchanged
  (single-query cursor format untouched; multi-query is a parallel path).
- **Original is variant 0** — recall never drops below the single-query baseline; re-rank anchors on the ORIGINAL query.
- **Cross-variant dedup** — a record surfaced by multiple variants appears ONCE.
- **Pagination correct** — composite-key offsets advance per (variant,source); continuation re-fans the frozen variants
  with NO LLM/expand; no overlap across pages; termination + empty-window guards hold.
- **Fail-soft** — no LLM (or expand failure) ⇒ single-query fallback + `errors["multi_query"]`; LLM never raises into
  the search path.
- **Cost bounded + disclosed** — `MAX_QUERY_VARIANTS` cap; documented N× fan-out.
- **Transparent** — `query_expansion` echoes the raw variants; ontology resolution still shown by `*_expansion`.
