# P3 — Resume + Parallel + Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use
> checkbox (`- [ ]`) syntax. Spec: `docs/superpowers/specs/2026-05-31-p3-resume-parallel-progress-design.md`.

**Goal:** Resumable + parallel `fetch` with optional MCP progress notifications; ship v0.14.0.

**Architecture:** Extract the per-file download into a coroutine, then layer resume (skip
verified-complete files), bounded-concurrency `asyncio.gather` (shared byte budget + per-task
partial cleanup, fail-loud preserved), and an optional `on_progress` callback wired by `server.py`
to MCP progress notifications.

**Conventions (verified live):** `fetch.fetch_files(client, resource, *, dest, files, max_bytes,
force, extract) -> FetchResult`. `FetchResult{paths, bytes, skipped}`. `_target_dir`, `_hasher`,
`_looks_like_html`, `_BINARY_MIMES`, `_CHUNK`, `archive.is_archive/extract_archive` already exist.
`server.py` low-level `Server`; `server.request_context` exposes `.meta` (progressToken) +
`.session.send_progress_notification(...)` — CONFIRM exact names against the installed `mcp` SDK
before wiring (Task 5). PostToolUse formatter reflows after each write — re-Read before a 2nd Edit
to the same region; fresh-read-guard blocks edits to unread files. Add imports in the same edit as
first usage (ruff `--fix` strips unused-then-used imports). Commit trailer (exactly):
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: `FetchResult.resumed` field

**Files:** `models.py` (`FetchResult`); `tests/test_models.py`.

- [ ] **Step 1:** failing test — `FetchResult(resumed=["a"]).resumed == ["a"]` and default `== []`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** add `resumed: list[str] = Field(default_factory=list)` to `FetchResult`.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5:** commit `feat(models): FetchResult.resumed`.

---

### Task 2: Extract `_download_one` (pure refactor, NO behavior change)

**Files:** `fetch.py`; `tests/test_fetch.py` (existing tests must stay green unchanged).

Goal: move the body of the `for f in selected` loop into a coroutine
`async def _download_one(client, f, target, *, budget, force, extract) -> _Outcome` returning a
small result object, and have `fetch_files` call it sequentially in a loop. Behavior identical —
this just creates the unit that Task 4 will parallelize.

- [ ] **Step 1:** Introduce a tiny outcome type + a shared budget:

```python
from dataclasses import dataclass, field

@dataclass
class _Budget:
    remaining: int
    force: bool
    lock: "asyncio.Lock" = field(default_factory=__import__("asyncio").Lock)

    async def debit(self, n: int, name: str) -> None:
        async with self.lock:
            if not self.force and n > self.remaining:
                raise FetchTooLargeError(
                    f"stream exceeded max_bytes while fetching {name}"
                )
            self.remaining -= n

@dataclass
class _Outcome:
    name: str
    path: str | None = None          # on-disk path (downloaded or resumed)
    extracted: list[str] = field(default_factory=list)
    bytes: int = 0
    state: str = "downloaded"        # downloaded | resumed | skipped
```

- [ ] **Step 2:** Move the existing per-file logic into `_download_one`, using `budget.debit(len(chunk), f.name)` in place of the old `written_total + written > max_bytes` inline check. Keep checksum verify, HTML sniff, unlink-on-failure, and the `extract` branch. Return `_Outcome(name, path=str(out), extracted=[...], bytes=written, state="downloaded")`; for no-URL/unsafe-name return `_Outcome(name, state="skipped")`.
- [ ] **Step 3:** Rewrite `fetch_files` to: pre-check `declared_total` (unchanged), build `budget = _Budget(remaining=max_bytes, force=force)`, then `for f in selected: outcome = await _download_one(...)` and assemble `FetchResult` from the outcomes (`paths` = every outcome with a path, `skipped` = state=="skipped", `bytes` = sum). Write the sidecar at the end (unchanged).
- [ ] **Step 4:** run `python -m pytest tests/test_fetch.py tests/test_fetch_gate.py -v` → ALL existing tests PASS unchanged (proves the refactor is behavior-preserving).
- [ ] **Step 5:** commit `refactor(fetch): extract _download_one coroutine + _Budget (no behavior change)`.

---

### Task 3: Resume — skip verified-complete files

**Files:** `fetch.py` (`_download_one`); `tests/test_fetch.py`.

- [ ] **Step 1:** failing tests:
  - pre-create `target/<name>` whose bytes hash to `f.checksum` → fetch skips it: outcome
    `state=="resumed"`, path set, and the MockTransport records NO request for that URL.
  - pre-create a file whose size matches `f.size` (no checksum) → resumed.
  - checksum mismatch on disk → re-downloaded (state=="downloaded").
  - no checksum AND no size → re-downloaded.
  - `force=True` → re-downloaded even when checksum matches.

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** add at the top of `_download_one` (after computing `out`, before streaming):

```python
def _already_complete(out: Path, f: FileEntry) -> bool:
    if not out.exists():
        return False
    if f.checksum and ":" in f.checksum:
        h = _hasher(f.checksum)
        if h is None:
            return False
        with out.open("rb") as fh:
            for block in iter(lambda: fh.read(_CHUNK), b""):
                h.update(block)
        return h.hexdigest() == f.checksum.split(":", 1)[1]
    if f.size is not None:
        return out.stat().st_size == f.size
    return False
```

In `_download_one`: `if not force and _already_complete(out, f): return _Outcome(f.name, path=str(out), state="resumed")`.

- [ ] **Step 4:** `fetch_files` assembles `resumed` = [o.name for o in outcomes if o.state=="resumed"] into the `FetchResult`. run tests → PASS.
- [ ] **Step 5:** commit `feat(fetch): resume — skip verified-complete files (idempotent re-fetch)`.

---

### Task 4: Parallelize downloads

**Files:** `fetch.py` (`fetch_files`); `tests/test_fetch.py`.

- [ ] **Step 1:** failing/again tests:
  - fetching 4 files writes all 4 (on disk + in `paths`); `paths`/`resumed`/`skipped` are stable
    (e.g. sorted) regardless of completion order.
  - a CONCURRENCY proof: serve files with a small per-chunk `asyncio.sleep`; assert wall-time
    `< 0.6 ×` the serial sum (proving overlap). (Use a generous bound to avoid flakiness.)
  - one file 404 → `fetch_files` raises `NotFoundError`; that file's partial is gone; the run does
    not hang (siblings cancelled cleanly).
  - under-declared stream blowing the shared budget → `FetchTooLargeError`.

- [ ] **Step 2:** run → FAIL (still sequential / order not guaranteed).
- [ ] **Step 3:** implement:

```python
_MAX_CONCURRENCY = 4

async def fetch_files(...):
    ...  # target, selected, declared_total pre-check, budget
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _guarded(f):
        async with sem:
            return await _download_one(client, f, target, budget=budget, force=force, extract=extract)

    outcomes = await asyncio.gather(*(_guarded(f) for f in selected))  # no return_exceptions: fail-loud
    # assemble FetchResult; sort lists for determinism
```

Ensure `_download_one` cleans its own partial on ANY exit-without-completion (wrap the stream/
write in `try: ... except BaseException: out.unlink(missing_ok=True); raise` so a `CancelledError`
from a sibling's failure also cleans up). Verify a completed file is NOT unlinked.

- [ ] **Step 4:** run `pytest tests/test_fetch.py tests/test_fetch_gate.py -v` then full `pytest -q` → PASS.
- [ ] **Step 5:** commit `feat(fetch): parallel downloads (bounded semaphore, shared budget, fail-loud)`.

---

### Task 5: Progress notifications (`on_progress` + server wiring)

**Files:** `fetch.py` (add `on_progress` param + invoke); `server.py` (`"fetch"` case); `tests/test_fetch.py`, `tests/test_server.py`.

- [ ] **Step 1:** failing tests:
  - `test_fetch.py`: pass an `on_progress` recorder (an async fn appending `(done,total,name)`);
    after fetching N files assert it was called N times with `done` increasing 1..N and `total==N`.
  - `test_server.py`: with a fake `request_context` carrying a `progressToken` + a fake session,
    the `"fetch"` dispatch triggers ≥1 `send_progress_notification`; with no token, zero calls and
    fetch still returns. (Mock `router.resolve` and `fetch_mod.fetch_files`, OR inject a fake
    session — match the existing test style in `test_server.py`.)

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement:
  - `fetch_files(..., on_progress: Callable[[int, int, str], Awaitable[None]] | None = None)`. Keep
    a completed counter incremented as each `_guarded` task finishes (e.g. wrap the gather with an
    `asyncio.as_completed` loop, or increment inside `_guarded` under a lock then `await
on_progress(done, total, name)`); call `on_progress` once per file with the running count. If
    `on_progress` is None, skip. Total = `len(selected)`.
  - `server.py` `"fetch"` case: before calling `fetch_files`, read the token:
    ```python
    ctx = server.request_context
    token = getattr(getattr(ctx, "meta", None), "progressToken", None)  # confirm names vs SDK
    async def _on_progress(done, total, name):
        if token is None:
            return
        try:
            await ctx.session.send_progress_notification(token, progress=done, total=total)
        except Exception as exc:  # auxiliary telemetry — log, never abort the fetch
            logger.warning("progress notification failed: %r", exc)
    ```
    Pass `on_progress=_on_progress` (or None when no token). VERIFY `send_progress_notification`'s
    real signature in the installed SDK and adapt (it may be `(progress_token, progress, total)`
    positional). A `logger` exists in `server.py` or add one.

- [ ] **Step 4:** run the two files + full `pytest -q` → PASS.
- [ ] **Step 5:** commit `feat(fetch,server): MCP progress notifications during fetch`.

---

### Task 6: Version bump to 0.14.0 + CHANGELOG

**Files:** `pyproject.toml:3`, `__init__.py:3`, `server.json` ×2, `CHANGELOG.md`, `tests/test_packaging.py`.

- [ ] **Step 1:** update `test_packaging.py` version → `0.14.0` (function name + 3 literals); run → FAIL.
- [ ] **Step 2:** bump the four synced places to `0.14.0`.
- [ ] **Step 3:** prepend CHANGELOG:

```markdown
## [0.14.0] - 2026-05-31

### Added

- `fetch` now downloads a resource's files in parallel (bounded concurrency).
- `fetch` resume — files already present and verified (by checksum, else size) are
  skipped and reported in `FetchResult.resumed`; a re-run is idempotent. `force=true`
  re-downloads everything.
- `fetch` emits MCP progress notifications as files complete when the caller supplies
  a `progressToken`.
```

- [ ] **Step 4:** full `pytest -q` → PASS. commit `chore: bump to 0.14.0 (resume + parallel + progress)`.

---

### Task 7: Real-execution / concurrency probe

**Files:** `tests/test_fetch.py` (concurrency proof — synthetic, always-on) + `tests/test_router.py` or `test_fetch.py` (`@_live_only` Zenodo re-fetch).

- [ ] **Step 1:** synthetic concurrency proof per Task 4 (kept as a permanent test). PLUS a
      `@_live_only` test: fetch a small multi-file Zenodo record to a temp dir, assert files land;
      fetch AGAIN to the same dir and assert every file is in `resumed` and `FetchResult.bytes == 0`.
- [ ] **Step 2:** run synthetic in the normal suite; run the live one with
      `DATA_AGGREGATOR_MCP_LIVE=1 ... -k <name>` → PASS.
- [ ] **Step 3:** commit `test: concurrency proof + live resume probe`.

---

## Final review

After all tasks: `pytest -q` green, dispatch a whole-branch code review (focus: concurrency
correctness — cancellation cleanup, shared-budget races, fail-loud under gather, progress
fail-soft), then `superpowers:finishing-a-development-branch` to merge and ship v0.14.0.
