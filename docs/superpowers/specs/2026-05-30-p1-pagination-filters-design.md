# P1 — Pagination + Faceted Filters (data-aggregator-mcp)

> **Status:** approved design (page-model fork decided by user 2026-05-30: `size` = total
> deduped results per page). Ships v0.12.0.

**Goal:** Let `search` walk past the first page of merged results (E1) and constrain
results by publication year and resource kind (E2), without changing the existing
single-page behavior for callers that pass neither a cursor nor a filter.

**Tech stack:** Python 3, httpx (async), pytest. Pure additions to existing adapters

- `router.search` + the `search` MCP tool schema.

---

## Background (live code, verified 2026-05-30)

- `router.search(client, query, *, size=10, sources=None, organism=None)` fans out
  `adapters[n].search(client, q, size=size)` via `asyncio.gather`, each returning
  `(adapter_total, list[DataResource])`, then `merged = _dedup(interleave(per_source))[:size]`,
  enriches, and returns `(total, merged, errors, expansion)`.
- `SearchResult` (models.py) already has `next_cursor: str | None = None` — **a dead field
  today; E1 populates it.**
- Adapter `search` signatures all take only `*, size: int`. **No offset/page param exists.**
- Upstream pagination is **heterogeneous**:
  - `omics` + `pubmed` ride NCBI eutils — `esearch` supports `retstart` (true row offset).
    `_eutils.esearch(client, db, term, retmax)` exists; it gains `retstart`.
  - `zenodo` (`page`+`size`), `datacite` (`page[number]`+`page[size]`), `openaire`
    (page-based) — **no raw row offset; page-based only.**
- `_merge.interleave(per_list)` round-robins lists preserving each list's order.
- `_dedup` (router) keys by lowercased DOI; native record beats `datacite:`-prefixed on
  collision; no-DOI records always kept.

---

## E1 — Pagination

### Contract

`search` gains an optional `cursor: str` argument. Two mutually-exclusive call modes:

1. **Fresh search** — caller passes `query` (+ optional `sources`/`organism`/filters/`size`).
   Returns page 1 and, if more results may exist, a `next_cursor`.
2. **Continuation** — caller passes only `cursor` (an opaque token from a prior
   `next_cursor`). All search parameters are read _from the cursor_; `query` and the other
   params are ignored when `cursor` is present. Returns the next page + a fresh
   `next_cursor` (or `null` when exhausted).

`size` keeps its current meaning: **up to `size` deduped, interleaved results per page.**
Passing both `cursor` and `query` is not an error — `cursor` wins (documented).

### Cursor token

Opaque, URL-safe base64 of a compact JSON object. Internal shape (never promised to
callers):

```json
{
  "q": "<query>",
  "sources": ["zenodo", "..."] | null,
  "organism": "<str>" | null,
  "filters": {"published_after": 2015, "published_before": 2020, "kind": "dataset"},
  "size": 10,
  "offsets": {"zenodo": 10, "datacite": 10, "omics": 10, "literature": 10}
}
```

`offsets` maps **adapter name → next row offset for that adapter**. Decoding a malformed or
non-base64 cursor raises `ValidationError` ("invalid or corrupt cursor") — fail loud, do not
silently restart from page 1.

### Per-adapter offset

Every adapter `search` gains `offset: int = 0`, fetching the window `[offset, offset+size)`:

- **omics / pubmed (NCBI):** thread `retstart=offset` through `_eutils.esearch`. Exact, one
  request. (omics fans to 3 dbs and pubmed is one backend of literature — each db/backend
  gets the same `offset`.)
- **zenodo / datacite / openaire (page-based):** the adapter computes
  `page = offset // size + 1`, requests that one page at page-size `size`, and returns
  `records[offset % size :]`. This yields the window from `offset` to the page end
  (≤ `size` records). When `offset` is not a multiple of `size` this re-fetches the head of
  the page and slices it off — the "slight re-fetch at page boundaries" the contract allows.
  `total_hits` is still returned from the same response.

`offset=0` reproduces today's exact request for every adapter (backward compatible).

### Router algorithm (`router.search`)

When `cursor` is given, decode it to recover `q/sources/organism/filters/size/offsets`;
otherwise `offsets` defaults to all-zero and the params come from the call.

1. Expand organism (unchanged) only on a fresh search; on continuation, reuse the frozen
   `q`/`organism` from the cursor (no re-expansion — keeps pages consistent).
2. For each selected adapter `n`: `adapter.search(client, q, size=size, offset=offsets[n])`
   → `(adapter_total, recs_n)` via `asyncio.gather(return_exceptions=True)` (existing
   error handling preserved — a failed adapter logs + contributes nothing).
3. Record record→adapter **origin** by object identity: `origin = {id(rec): n}` for every
   `rec` in every `recs_n`. (Used to count per-adapter consumption without disturbing
   `interleave`/`_dedup`, which keep operating on plain `list[DataResource]` and return the
   _same objects_ — identity is stable.)
4. `merged = _dedup(interleave(per_source))` — the full deduped **candidate** list in merge
   order (NOT truncated yet). Unchanged interleave/dedup semantics.
5. **Walk `merged` to the cut point**, applying E2 filters as you go:
   - iterate `merged` with index `i`, keeping `emitted = []`;
   - if `passes_filters(rec)`: append to `emitted`; if `len(emitted) == size`, set
     `cut = i` and **stop**;
   - else (filter-rejected): continue (it is _consumed_ — see below);
   - if the loop ends without hitting `size`, `cut = len(merged) - 1`.
     The emitted page is `emitted` (≤ `size`). `consumed = merged[: cut + 1]`.
6. `consumed_per_adapter = Counter(origin[id(rec)] for rec in consumed)` — counts records
   each adapter contributed to the consumed prefix, **including filter-rejected ones**. This
   is the fix for the filter-stall: a fully-filtered page still advances offsets, so
   pagination never loops on the same window.
7. `new_offsets[n] = offsets[n] + consumed_per_adapter[n]`. Adapters whose records were
   over the size budget (after `cut`) are NOT advanced past them → no recall loss. Dups
   dropped by `_dedup` are absent from `merged`, so they are re-fetched + re-deduped next
   page (the allowed "rare cross-page dup", bounded by one window).
8. `more = (cut < len(merged) - 1) or any(new_offsets[n] < adapter_total[n] for each adapter n)`
   — leftover fetched candidates OR any source whose advanced offset is still short of its
   upstream total. (Do **not** use `len(recs_n) == size`: the offset-slice makes a page-based
   adapter return fewer than `size` records mid-stream even when it has more, which would stop
   pagination prematurely. Compare the advanced offset against the upstream total instead.)
9. `next_cursor = encode({q, sources, organism, filters, size, offsets: new_offsets})`
   if `more` else `None`. Set it on `SearchResult.next_cursor`.
10. Enrich the emitted page (unchanged `_enrich`). Return
    `(total, page_records, errors, expansion)` as today; `total` stays the summed upstream
    estimate (pre-filter) and is documented as such.

`enrich`/`compact`/`interleave`/`_dedup` semantics are otherwise untouched, so page 1 of a
fresh, unfiltered, relevance search (no `cut` before the end, all records pass) is
byte-identical to today.

---

## E2 — Faceted filters

Three optional `search` params, all also frozen into the cursor:

- `published_after: int` — keep records with `year >= published_after`.
- `published_before: int` — keep records with `year <= published_before`.
- `kind: str` — keep records with `kind == kind`. Valid values are the existing
  `DataResource.kind` values; an unknown value is a `ValidationError` (fail loud).

Filtering is **post-fetch, post-dedup, pre-truncate** (step 5 above), applied to the
normalized `DataResource.year` / `.kind`. A record with `year is None` is **dropped** when
either year bound is set (cannot prove it satisfies the bound — fail toward exclusion, and
document it).

**Known limitation (documented, not a bug):** because filtering happens on the fetched
window rather than pushed to each upstream API, a heavily-filtered query can return fewer
than `size` results per page even when more matching records exist deeper. Upstream
filter push-down is deferred (see Backlog). `next_cursor` still advances, so callers can
page through to reach them.

### `list_sources`

Each source's `filters_supported` gains `"published_after"`, `"published_before"`, `"kind"`
(all sources support them via post-filter). Add `"cursor"` to advertise pagination.

---

## Deferred to later tiers (explicitly out of P1)

- **`sort` (e.g. `newest`)** — correct cross-page ordering needs per-source upstream sort
  push-down (offset cursors don't globally order a merged feed). Pairs with filter
  push-down. → future "search push-down" item.
- **`funder` filter** — requires funding metadata not present until D2 (P2).
- **Filter/sort push-down into upstream APIs** — efficiency optimization; MVP post-filters.

---

## Testing

Unit (synthetic httpx mock, per existing `tests/` patterns):

- `test__eutils.py`: `esearch` sends `retstart` when offset given; omitted/0 unchanged.
- `test_zenodo.py` / `test_datacite.py` / `test_openaire.py`: `offset` → correct
  `page`/`page[number]`; non-aligned offset slices the page tail; `offset=0` unchanged.
- `test_omics.py` / `test_pubmed.py`: `offset` threads to `retstart` for each db/backend.
- `test_router.py`:
  - fresh search sets `next_cursor` when an adapter returns a full `size` window; `None`
    when all short.
  - continuation decodes cursor, advances offsets, returns next window.
  - emitted-per-adapter offset advance is correct across a 2-page walk (no skipped rows
    beyond the allowed dup re-fetch).
  - malformed cursor → `ValidationError`.
  - year-range filter drops out-of-range + `year is None` records; `kind` filter; unknown
    kind → `ValidationError`.
  - page 1 unfiltered relevance == today's output (regression guard).
- `test_server.py`: `search` inputSchema exposes `cursor`/`published_after`/
  `published_before`/`kind`; `_dispatch` threads them; `list_sources` advertises them.
- `test_models.py`: cursor encode/decode round-trips; reject corrupt token.

**Real-execution probe (boundary):** one live (network) walkthrough paging Zenodo+DataCite
to page 2 for a known query, asserting page 2 ≠ page 1 and offsets advanced. Gated like the
repo's other live probes (skip when offline).

## Version

Bump 0.11.0 → 0.12.0 in the 4 synced places (`pyproject.toml`, `__init__.py`, `server.json`
×2) + `test_packaging` assertion + a new `CHANGELOG.md` section.
