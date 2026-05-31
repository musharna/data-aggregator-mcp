# P5 — Pre-scale hardening + semantic re-rank (data-aggregator-mcp)

> **Status:** approved direction (user 2026-05-31: include E4; per-service token bucket;
> resolve+taxonomy cache @1h; opt-in health probe; semantic re-rank via optional remote endpoint).
> Ships **v0.16.0**. Prior tiers P1–P4 shipped via spec→plan→subagent-driven TDD→review→merge --no-ff.

**Goal:** Make the server safe to run under real agent load (rate-limit pacing, response caching,
health visibility) and add an optional semantic re-rank of search results — without adding a fifth
tool (the 4-tool ceiling holds) or a required API key / ML dependency.

**Tech stack:** Python 3, httpx (async), pytest. Four new small modules (`_ratelimit.py`,
`_cache.py`, `health.py`, `embeddings.py`), each one responsibility; wired into the existing
`_http`/`router`/`server` chokepoints. No new third-party dependency.

---

## Background (live code, verified 2026-05-31)

- **Single HTTP chokepoint:** every upstream call funnels through `_http._retrying`
  (`_http.py:26`), which already does transport+429/5xx+parse retry and status→typed-error. It
  takes a `service: str` label. The actual send is `client.request(...)` at `_http.py:51` inside a
  `for attempt in range(max_retries)` loop. This is the one place to acquire a rate-limit token.
- **`service` labels are descriptive, not canonical:** e.g. `"NCBI esearch (geo)"`,
  `"NCBI efetch (sra)"`, `"NCBI elink (...)"`, `"NCBI esummary (...)"`, `"NCBI idconv"`,
  `"EuropePMC search"`, `"Zenodo search"`, `"DataCite search"`, `"OpenAIRE"`, `"HuggingFace search"`,
  `"Unpaywall"`, etc. So bucket selection needs a **prefix classifier**, not an exact match.
- **NCBI's rate limit is per-account across all eutils endpoints** (3 req/s anon, 10 with an API
  key) — so all `NCBI*` services must share ONE bucket, not one bucket per endpoint.
- **Fresh client per call, long-lived process:** `server.py:334` opens
  `async with httpx.AsyncClient(...)` per `_dispatch`. So client-scoped state does NOT persist, but
  **module-level state does** (the stdio process outlives individual tool calls). `taxonomy._CACHE`
  (`taxonomy.py:49`, an unbounded `dict`) already relies on this. Rate-limiter buckets and the TTL
  cache therefore live at module level.
- **Three unbounded fan-outs** today: `router.search_page` gather over 5 adapters
  (`router.py:232`), `literature.search` gather (`literature.py:37`), `omics.search` gather
  (`omics.py:137`). None paces req/s; a broad search+organism can fire >3 NCBI calls in well under a
  second → 429s under real load. The token bucket (acquired in `_http`) fixes all three at once
  because they all route through `_http`.
- **`router.resolve`** (`router.py:328`) routes by id shape, then does additive taxa/link
  enrichment. Caching wraps the whole function keyed on `resource_id`.
- **`router.search_page`** (`router.py:182`) fetches a per-source window, dedups by DOI
  (`_dedup`), round-robin interleaves (`_merge.interleave`), then advances per-source offsets by the
  consumed prefix up to the size-th passing record and packs them into the opaque `next_cursor`. The
  re-rank hooks in between "merged candidates" and "return top size".
- **`list_sources`** (`server.py:332`) returns the static `_SOURCES` list (`server.py:81`); no
  network. The health probe is an opt-in extension of this handler.

---

## E3 — Per-service token-bucket rate limiter (`_ratelimit.py`, new)

**Responsibility:** pace outbound req/s per upstream so we never trip a documented rate limit.

- `class TokenBucket`: classic async token bucket. Fields: `rate` (tokens/sec), `capacity`
  (burst, default = `max(rate, 1)`), `_tokens` (float), `_updated` (monotonic timestamp). Method
  `async def acquire(self)`: refill `min(capacity, _tokens + (now - _updated) * rate)`, then if
  `_tokens >= 1` consume one and return; else `await asyncio.sleep((1 - _tokens) / rate)` and retry.
  A single `asyncio.Lock` serializes the refill/consume critical section so concurrent fan-out
  callers can't double-spend. Uses `time.monotonic()` (runtime code — `Date.now` ban applies only to
  workflow scripts, not the package).
- Module-level registry `_BUCKETS: dict[str, TokenBucket]` + `_bucket_for(service: str) -> str`
  classifier: `service.startswith("NCBI")` → `"ncbi"`, else `"default"`. (Room to add more named
  buckets later; only NCBI needs strict pacing today.)
- `_RATES: dict[str, float]` = `{"ncbi": 3.0, "default": 10.0}`. NCBI rate is bumped to `10.0` when
  `NCBI_API_KEY` **or** `NCBI_EMAIL` is set in the environment (read once at bucket-creation time).
- `async def acquire(service: str) -> None`: look up / lazily create the bucket for
  `_bucket_for(service)` (rate from `_RATES`, capacity = ceil(rate)), then `await bucket.acquire()`.
- A test seam: `acquire` and `TokenBucket` accept an injectable `now` callable defaulting to
  `time.monotonic`, so tests pace deterministically without real sleeps. `reset()` clears
  `_BUCKETS` for test isolation.

**Wiring:** in `_http._retrying`, immediately before `client.request(...)` (inside the attempt
loop, so a retry re-acquires a token — a 429-retry storm cannot itself exceed the rate):
`await _ratelimit.acquire(service)`.

---

## F2 — Shared TTL+LRU cache (`_cache.py`, new)

**Responsibility:** avoid re-hitting upstreams for stable, repeatedly-requested metadata.

- `class TTLCache`: `__init__(self, maxsize: int = 512, ttl: float = 3600.0, now=time.monotonic)`.
  Backed by an `OrderedDict`. `get(key)` → value or a sentinel `MISS` if absent/expired (expired
  entries are evicted on access); a hit moves the key to MRU. `set(key, value)` inserts/refreshes,
  evicts LRU when over `maxsize`. `ttl <= 0` disables caching (`get` always MISS, `set` is a no-op).
  `clear()` for test isolation.
- Default TTL = `3600.0`s, overridable via env `CACHE_TTL_SECONDS` (parsed once; `0` disables).
- **resolve caching:** `router.resolve` checks a module-level `_RESOLVE_CACHE` keyed on the
  normalized `resource_id` (`rid`) BEFORE routing; on miss, runs the existing body and stores the
  result. Cache stores the fully-enriched `DataResource`. (Errors are NOT cached — a failed resolve
  re-attempts next call.)
- **taxonomy migration:** replace `taxonomy._CACHE: dict` with a `TTLCache` instance
  (`maxsize` generous, same TTL). `resolve_taxon` uses `get`/`set`; the `None` (not-found) result is
  still cached as a real stored value (negative caching preserved — currently `_CACHE[key] = None`),
  using a distinct sentinel so "stored None" ≠ "missing".

**Not cached:** `search()` — results are volatile and cursor-stateful; caching them risks staleness
and cursor-coherence bugs for little gain. Explicitly out of scope (deferred).

---

## F3 — Health probe folded into `list_sources` (`health.py`, new)

**Responsibility:** let a caller check upstream reachability without a fifth tool.

- `_PROBE_TARGETS: dict[str, str]` maps each `_SOURCES` name → a cheap base endpoint to probe:
  `zenodo` → `https://zenodo.org/api/records?size=1`, `datacite` →
  `https://api.datacite.org/heartbeat`, `omics` →
  `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi`, `literature` →
  `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=test&format=json&pageSize=1`,
  `huggingface` → `https://huggingface.co/api/datasets?limit=1`. (Verified-reachable endpoints;
  each is a documented, low-cost GET.)
- `async def probe_sources(client) -> list[dict]`: probe all targets concurrently (a `gather`).
  Each probe is a **direct timed GET** (`client.request` with a short timeout, e.g. 5s) — NOT the
  `_http` retry path, because a down endpoint should report fast, not after 3 backoff retries. For
  each: `{"name", "status": "up"|"down", "latency_ms": int|None, "detail": <short error if down>}`.
  `up` = any 2xx/3xx within timeout; everything else (4xx/5xx/timeout/transport) = `down` with a
  short reason. A probe NEVER raises — health is best-effort observability. (Probes do not acquire a
  rate-limit token: they are infrequent, opt-in, and one-shot per source.)
- **Wiring:** `list_sources` tool gains an optional `check_health: bool` input (default `false`).
  When false (default), the handler returns the current static `{"sources": _SOURCES}` with NO
  network — contract unchanged. When true, it runs `probe_sources` and merges each result into the
  matching source entry under a `"health"` key.

---

## E4 — Semantic re-rank (`embeddings.py`, new) + `search` gains `rank`

**Responsibility:** optionally reorder a search page by semantic similarity to the query, using a
remote embedding endpoint. No local index, no required key, no ML dependency.

- **Config (env, read at call time):** `EMBEDDING_API_BASE` (e.g. `https://api.openai.com/v1`),
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` (default `text-embedding-3-small`). All three optional;
  `EMBEDDING_API_BASE` absent ⇒ feature disabled (fail-soft).
- `async def embed(client, texts: list[str]) -> list[list[float]] | None`: returns `None` when
  `EMBEDDING_API_BASE` is unset. Otherwise POSTs `{"model": MODEL, "input": texts}` to
  `{BASE}/embeddings` with `Authorization: Bearer {KEY}` (header omitted if no key — supports
  keyless local servers), through `_http.request_json` (so it inherits retry + the E3 limiter,
  service label `"embeddings"`). Parses `data[].embedding` in order. Any failure → the caller treats
  it as "unavailable" (returns `None` up the chain) rather than raising.
- `def cosine_rank(query_vec, cand_vecs) -> list[int]`: returns candidate indices sorted by
  descending cosine similarity (pure function, unit-testable, no I/O). Zero-norm vectors sort last.
- `async def rerank(client, query: str, resources: list[DataResource]) -> tuple[list[DataResource], str | None]`:
  builds one text per resource (`f"{title}\n{description}"`, truncated to a sane char cap, e.g.
  2000), calls `embed([query, *texts])` in ONE batched request, cosine-ranks, returns the reordered
  list. On `None`/failure returns `(resources_unchanged, reason_string)`; on success returns
  `(reordered, None)`.

- **`search` `rank` param:** enum `"relevance"` (default = current behavior) | `"semantic"`.
- **`router.search_page` integration:** after dedup+interleave produces the merged candidate list,
  if `rank == "semantic"`:
  1. `reordered, reason = await embeddings.rerank(client, query, merged)`.
  2. If `reason` is not None (unavailable/failed), keep keyword order and set
     `errors["semantic"] = reason` — the search still succeeds.
  3. Take the top `size` of the (re)ordered list as the page.
  4. **Pagination contract in semantic mode is window-based:** each source's offset advances by the
     FULL fetched window (everything fetched this page), not the consumed-prefix-up-to-size used in
     relevance mode — because ranking needs all candidates, so all were consumed. `next_cursor`
     still walks deeper; `more` keyed off upstream totals as today. Relevance mode is unchanged.
     This contract difference is documented in the `rank` param description and the CHANGELOG.

---

## Files

- Create: `src/data_aggregator_mcp/_ratelimit.py`, `_cache.py`, `health.py`, `embeddings.py`.
- Modify: `_http.py` (acquire token before send), `taxonomy.py` (migrate `_CACHE` → `TTLCache`),
  `router.py` (resolve cache wrap; `search_page` `rank` param + rerank hook + window-based offset
  advance in semantic mode; `search` passthrough), `server.py` (`list_sources` `check_health`
  param + handler; `search` tool `rank` param + dispatch passthrough).
- Tests: `tests/test_ratelimit.py`, `test_cache.py`, `test_health.py`, `test_embeddings.py` (new);
  extend `test_router.py` (resolve cache hit/miss; semantic rerank ordering + fail-soft + window
  pagination), `test_server.py` (`check_health` true/false; `rank` param plumbed), `test_taxonomy.py`
  (cache migration: hit/miss/negative/expiry), `test_packaging.py` (version).

---

## Testing strategy

**Unit (synthetic, `pytest_httpx` / injected clock):**

- `_ratelimit`: injected `now` clock — N `acquire()` calls on a 3/s bucket span ≥ (N-capacity)/3
  simulated seconds; concurrent acquires don't double-spend (lock); `NCBI_API_KEY` set → 10/s;
  `NCBI*` services share one bucket (two different NCBI service labels draw from the same tokens).
- `_cache`: hit within TTL; miss after expiry (injected clock); LRU eviction at maxsize; `ttl<=0`
  disables; negative value stored vs missing distinguished.
- `taxonomy`: resolve_taxon hit/miss/negative-cache/expiry on the migrated cache.
- `health`: mock a 2xx target → `up` + latency; mock a 500/timeout → `down` + reason; probe never
  raises.
- `embeddings`: `embed` returns `None` when `EMBEDDING_API_BASE` unset; parses `data[].embedding`
  order when set; `cosine_rank` orders a hand-built case correctly (incl. zero-norm last);
  `rerank` reorders on success, returns `(unchanged, reason)` on unavailable/error.
- `router`/`server`: resolve served from cache on 2nd call (upstream mock asserts called once);
  `search(rank='semantic')` with mocked embeddings reorders; with embeddings unconfigured →
  keyword order + `errors['semantic']`; `list_sources(check_health=true)` merges health, default
  call stays network-free.

**Real-execution probes (`DATA_AGGREGATOR_MCP_LIVE=1`, gated/skipped otherwise):**

- Live `list_sources(check_health=true)` against the real endpoints → every source resolves to
  `up`/`down` with a numeric latency for the ups (network-tolerant: asserts shape, not all-up).
- Live rate-limiter: fire ~6 real NCBI esearch calls and assert wall-clock ≥ ~1s (3/s pacing) —
  the real-execution check that the limiter actually paces the real CLI path, not just a mock.
- Semantic rerank against a real `/v1/embeddings` ONLY if `EMBEDDING_API_BASE` is set in the env;
  else `pytest.skip` (no key is bundled).

## Version

Bump 0.15.0 → 0.16.0 in the 4 synced places (`pyproject.toml:3`, `server.json:10` + `:16`,
`src/data_aggregator_mcp/__init__.py:3`) + `tests/test_packaging.py` (3 asserts) + a new
`CHANGELOG.md` `[0.16.0]` section.

## Deferred / out of scope

- Local/bundled embedding backend (env-pluggable as a later option).
- Caching `search()` result pages.
- NL→structured query rewrite (the other E4 model; not chosen).
- Per-source rate buckets beyond NCBI (only NCBI needs strict pacing today; classifier leaves room).
