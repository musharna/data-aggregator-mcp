# P3 — Resumable + Parallel Fetch + Progress Notifications (data-aggregator-mcp)

> **Status:** approved design (user 2026-05-31: B2 re-scoped from a job queue to MCP progress
> notifications — a stdio MCP can't persist jobs; progress + idempotent resume is the idiomatic
> model. 4-tool cap kept as a discipline, not a hard rule.). Ships v0.14.0.

**Goal:** Make `fetch` (a) resume — skip files already present and verified, so an interrupted
fetch re-run is cheap and idempotent (B3); (b) parallel — download the selected files
concurrently under a bounded semaphore (B3); (c) observable — emit MCP progress notifications as
files complete when the caller supplies a `progressToken` (B2, re-scoped). No new tools.

**Tech stack:** Python 3, httpx (async), asyncio, pytest. All changes are in `fetch.py`
(+`models.py` for the new result field) and `server.py` (progress wiring).

---

## Background (live code, verified 2026-05-31)

- `fetch.fetch_files(client, resource, *, dest, files, max_bytes, force, extract) -> FetchResult`
  is a **sequential** `for f in selected` loop. Each file streams to `target/safe_name`, with a
  global running-total `max_bytes` guard (`written_total + written > max_bytes`), checksum
  verification, HTML-sniff for unchecksummed binaries, and per-file `out.unlink()` on failure. A
  single file's HTTP/transport error raises and aborts the whole fetch (fail-loud).
- It ALWAYS re-downloads (overwrites) — there is no skip-if-already-complete.
- `FetchResult{paths: list[str], bytes: int, skipped: list[str]}`. `skipped` = files with no
  usable URL / unsafe name.
- Sidecar `target/.dataresource.json` written at the end.
- `server.py` uses the low-level `mcp.server.Server`; `_dispatch(name, args)` has no context, but
  `server.request_context` (a contextvar set during a call) exposes `.meta` (carrying an optional
  `progressToken`) and `.session` (with `send_progress_notification(progressToken, progress,
total)`). Verify the exact attribute/method names against the installed `mcp` SDK before wiring.

---

## B3 — Resume (idempotent re-fetch)

Before downloading file `f`, if its target file already exists AND can be **verified complete**,
skip the download and record it in a new `FetchResult.resumed` list:

- **Verify-complete predicate** (`_already_complete(out, f)`):
  - if `f.checksum` present: hash the on-disk file and compare → complete iff match;
  - elif `f.size` is not None: complete iff `out.stat().st_size == f.size`;
  - else (no checksum, no size): cannot verify → NOT complete (re-download — safe default).
- `force=True` disables resume entirely (always re-download), preserving today's behavior.
- Skipped-as-complete files still contribute their on-disk path to `paths` (the caller wants the
  path regardless) and are additionally listed in `resumed`. Their bytes are NOT added to
  `written_total` (nothing was transferred).

`FetchResult` gains `resumed: list[str] = []`. `paths` continues to list every file now on disk
(downloaded + resumed). `skipped` keeps its current meaning (no URL / unusable).

**Behavior change (documented):** an unforced re-fetch now skips already-complete files instead
of re-downloading them. This is the intended idempotency. `force=true` restores always-download.

## B3 — Parallel downloads

Replace the sequential loop with bounded-concurrency `asyncio.gather` over per-file download
coroutines:

- A module constant `_MAX_CONCURRENCY = 4` gates an `asyncio.Semaphore`. (Internal — not a tool
  param; YAGNI.)
- Each coroutine does the existing per-file work (resume check → stream → checksum → HTML-sniff →
  optional extract) and returns its outcome (`downloaded`/`resumed`/`skipped` + path + bytes).
- **`max_bytes` under concurrency:** keep the upfront `declared_total > max_bytes` pre-check.
  For the streaming guard, use a shared `_Budget` (an `int` remaining + `asyncio.Lock`): each
  task, before writing a chunk, atomically debits `len(chunk)`; if remaining would go negative
  and not `force`, the task raises `FetchTooLargeError`. This preserves today's fail-loud guard
  against under-declared sizes.
- **Failure semantics (fail-loud preserved):** use `asyncio.gather(*tasks)` WITHOUT
  `return_exceptions`, so the first failing file propagates and the run fails. Each task MUST
  clean up its own partial output on exception OR cancellation (`try/except/finally:
out.unlink(missing_ok=True)` when the write didn't complete) — concurrency means sibling tasks
  may be mid-write when one fails and gets cancelled. Files that completed before the failure
  remain on disk (same as today).
- The sidecar `.dataresource.json` is written once after all downloads succeed.

Determinism: `paths`/`resumed`/`skipped` are sorted (or assembled in `selected` order) so the
result is stable regardless of completion order.

## B2 — Progress notifications (re-scoped)

`fetch_files` gains an optional `on_progress: Callable[[int, int, str], Awaitable[None]] | None`.
It is invoked each time a file reaches a terminal state (downloaded / resumed / skipped) with
`(completed_count, total_count, last_name)`. `fetch.py` stays transport-agnostic — it knows
nothing about MCP.

`server.py` builds the callback in the `"fetch"` case:

- Read the progress token from `server.request_context.meta` (the field is typically
  `progressToken`; confirm against the SDK). If absent → pass `on_progress=None` (no-op).
- If present → `on_progress` calls
  `server.request_context.session.send_progress_notification(progress_token, progress=done,
total=total)`. A notification failure is logged and swallowed — progress is auxiliary telemetry;
  it must NOT abort or mask the actual download (this is the one sanctioned non-fail-loud spot,
  because the core operation still succeeds and the failure is logged, not hidden).

---

## Deferred / out of scope

- Persistent cross-process job queue + status tool (the rejected B2 interpretation).
- Byte-level (sub-file) progress — file-level granularity only this tier.
- Parallel-across-resources (we parallelize files within one resource's fetch).

---

## Testing

Unit (synthetic httpx `MockTransport` serving multiple file URLs):

- `test_fetch.py`:
  - resume: a pre-existing file matching `checksum` is skipped (in `resumed`, not re-downloaded —
    assert the transport saw no request for it); matching `size` (no checksum) skipped; mismatched
    checksum / unknown size+no-checksum re-downloaded; `force=true` re-downloads even when complete.
  - parallel: fetching N files writes all N (assert all on disk + in `paths`); result lists are
    order-stable.
  - max_bytes under concurrency: an under-declared stream that blows the shared budget raises
    `FetchTooLargeError` and leaves no oversized file; `force` overrides.
  - failure cleanup: one file 404s → whole fetch raises `NotFoundError`/`UpstreamUnavailableError`,
    and that file's partial is removed (sibling completed files may remain).
  - progress: an `on_progress` recorder is called once per file with monotonically increasing
    `done` up to `total`.
- `test_server.py`: with a request carrying a `progressToken`, the `"fetch"` dispatch sends ≥1
  progress notification (assert via a fake session); without a token, none are sent and fetch
  still works. (Mock `router.resolve` + `fetch_mod.fetch_files`.)
- `test_models.py`: `FetchResult.resumed` round-trips.

**Real-execution / concurrency check (boundary):** a `MockTransport` (or a tiny local httpx app)
serving several files with artificial per-chunk delay, fetched with `_MAX_CONCURRENCY > 1`,
asserting (a) all files land intact, (b) wall-time is meaningfully below the serial sum (proving
real concurrency), (c) a concurrent run with one failing URL cleans the partial and raises. A live
(`DATA_AGGREGATOR_MCP_LIVE=1`) probe fetching a small multi-file Zenodo record, then re-fetching
to confirm the second run reports them all `resumed` (zero bytes transferred).

## Version

Bump 0.13.0 → 0.14.0 (4 synced places + `test_packaging` + a `CHANGELOG.md` section).
