"""Stream files to a content-addressed disk cache. Returns paths, never bytes."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from data_aggregator_mcp import archive
from data_aggregator_mcp.errors import FetchTooLargeError, NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, FetchResult, FileEntry

DEFAULT_MAX_BYTES = 2_000_000_000  # ~2 GB
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "data-aggregator-mcp"
_CHUNK = 1 << 16  # 64 KiB
_MAX_CONCURRENCY = 4  # bounded parallel downloads per resource

# Declared mimes whose body must NOT be HTML (the classic "paywall/login page
# served in place of the file" failure for unverified downloads).
_BINARY_MIMES = ("application/pdf", "application/xml", "text/xml")


def _looks_like_html(head: bytes) -> bool:
    sniff = head[:512].lstrip().lower()
    return sniff.startswith((b"<!doctype html", b"<html"))


def _target_dir(resource: DataResource, dest: str | None) -> Path:
    base = Path(dest) if dest else DEFAULT_CACHE_DIR
    safe = resource.id.replace(":", "/").replace("..", "_")
    return base / safe


def _hasher(checksum: str | None):
    if not checksum or ":" not in checksum:
        return None
    algo = checksum.split(":", 1)[0]
    if algo not in hashlib.algorithms_available:
        return None
    return hashlib.new(algo)


def _already_complete(out: Path, f: FileEntry) -> bool:
    """True iff the on-disk file can be *verified* complete: checksum match when
    a checksum is declared, else size match when ``f.size`` is known. With
    neither signal we cannot verify → re-download (safe default)."""
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


@dataclass
class _Budget:
    """Shared byte budget across (eventually concurrent) downloads. ``debit``
    is serialized by a lock so the running total can't race; over-budget raises
    ``FetchTooLargeError`` unless ``force``."""

    remaining: int
    force: bool
    cap: int  # original max_bytes (used as the per-archive extraction ceiling)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def debit(self, n: int, name: str) -> None:
        async with self.lock:
            if not self.force and n > self.remaining:
                raise FetchTooLargeError(f"stream exceeded max_bytes while fetching {name}")
            self.remaining -= n

    async def headroom(self) -> int:
        """Return remaining bytes under the lock (snapshot for extraction ceiling)."""
        async with self.lock:
            return self.remaining


@dataclass
class _Outcome:
    name: str
    path: str | None = None  # on-disk path (downloaded or resumed)
    extracted: list[str] = field(default_factory=list)
    bytes: int = 0
    state: str = "downloaded"  # downloaded | resumed | skipped


async def _download_one(
    client: httpx.AsyncClient,
    f: FileEntry,
    target: Path,
    *,
    budget: _Budget,
    force: bool,
    extract: bool,
) -> _Outcome:
    """Download a single file into ``target``, verifying checksum / sniffing for
    HTML decoys, cleaning up its partial on failure, and optionally extracting an
    archive. Returns an ``_Outcome`` describing what happened."""
    if not f.url:
        return _Outcome(f.name, state="skipped")
    # Fix 3: reject non-http/https schemes before any I/O (poisoned metadata guard).
    from urllib.parse import urlparse as _urlparse

    _scheme = _urlparse(f.url).scheme.lower()
    if _scheme not in ("http", "https"):
        raise UpstreamUnavailableError(
            f"fetch {f.name}: URL scheme {_scheme!r} is not allowed "
            f"(only http/https); refusing to fetch {f.url}"
        )
    # Sanitize: f.name is uploader-controlled (Zenodo file key). Reduce to a
    # bare basename so it cannot escape the target dir via path traversal.
    safe_name = Path(f.name).name
    if not safe_name or safe_name in (".", ".."):
        return _Outcome(f.name, state="skipped")
    out = target / safe_name
    if not force and _already_complete(out, f):
        return _Outcome(f.name, path=str(out), state="resumed")
    h = _hasher(f.checksum)
    written = 0
    first_head = b""
    first_chunk_seen = False
    # Concurrency: a sibling task's failure cancels this one mid-stream
    # (CancelledError). ANY escape before the file is verified-complete must
    # remove our partial — but a completed file must survive. ``complete`` flips
    # only on the success path; the broad ``except BaseException`` cleans up
    # otherwise (covers cancellation, which the typed handlers below do not).
    complete = False
    try:
        try:
            async with client.stream("GET", f.url, timeout=300.0) as resp:
                resp.raise_for_status()
                with out.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        await budget.debit(len(chunk), f.name)
                        written += len(chunk)
                        if not first_chunk_seen:
                            first_head = chunk[:512]
                            first_chunk_seen = True
                        if h is not None:
                            h.update(chunk)
                        fh.write(chunk)
        except httpx.HTTPStatusError as exc:
            sc = exc.response.status_code
            if sc == 404:
                raise NotFoundError(f"fetch {f.name} → HTTP 404 ({f.url})") from exc
            raise UpstreamUnavailableError(f"fetch {f.name} → HTTP {sc} ({f.url})") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailableError(f"fetch {f.name} transport failure: {exc!r}") from exc
        if h is not None and f.checksum:
            expected = f.checksum.split(":", 1)[1]
            if h.hexdigest() != expected:
                raise UpstreamUnavailableError(f"checksum mismatch for {f.name}")
        if h is None and f.mime in _BINARY_MIMES and _looks_like_html(first_head):
            raise UpstreamUnavailableError(
                f"fetch {f.name}: body is HTML, not the declared {f.mime} "
                "(the URL likely served a login/paywall/error page)"
            )
        extracted: list[str] = []
        if extract and archive.is_archive(safe_name):
            # Fix 2: pass the REMAINING budget headroom as the extraction ceiling so
            # that download + extraction together cannot exceed max_bytes. When force
            # is set we keep the original cap (unlimited extraction, matching prior
            # behaviour). Debit the actual extracted bytes from the budget afterward
            # so subsequent archives in the same fetch share the same ceiling.
            if force:
                extract_max = budget.cap
            else:
                extract_max = await budget.headroom()
            members = archive.extract_archive(out, target, max_bytes=extract_max)
            extracted_bytes = sum(p.stat().st_size for p in members)
            await budget.debit(extracted_bytes, safe_name)
            extracted = [str(m) for m in members]
        complete = True
        return _Outcome(
            f.name, path=str(out), extracted=extracted, bytes=written, state="downloaded"
        )
    except BaseException:
        if not complete:
            out.unlink(missing_ok=True)
        raise


async def fetch_files(
    client: httpx.AsyncClient,
    resource: DataResource,
    *,
    dest: str | None = None,
    files: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    force: bool = False,
    extract: bool = False,
    on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
) -> FetchResult:
    """Download ``resource`` files to disk. ``files`` is an optional glob over
    file names. Fails loud over ``max_bytes`` unless ``force``. Verifies
    checksums when present; writes a ``.dataresource.json`` provenance sidecar.

    ``on_progress`` (optional) is awaited once per file as it reaches a terminal
    state, with ``(completed_count, total_count, last_name)`` where ``total`` is
    ``len(selected)`` and ``completed_count`` runs 1..total. Completion order
    under concurrency is nondeterministic, so the count is monotonic but file
    names arrive in finish order. None → no callback. ``on_progress`` is part of
    the fetch's own success path: if it raises, that raises (callers wanting
    fail-soft telemetry must swallow inside their callback).
    """
    target = _target_dir(resource, dest)
    target.mkdir(parents=True, exist_ok=True)

    selected = resource.files
    if files:
        selected = [f for f in resource.files if fnmatch.fnmatch(f.name, files)]

    declared_total = sum(f.size or 0 for f in selected)
    if declared_total > max_bytes and not force:
        raise FetchTooLargeError(
            f"selected files total {declared_total} bytes exceed max_bytes={max_bytes}; "
            "pass force=true to override"
        )

    budget = _Budget(remaining=max_bytes, force=force, cap=max_bytes)

    # Download up to _MAX_CONCURRENCY files at once. The first failure
    # propagates = fail-loud; the remaining in-flight tasks are cancelled and
    # each cleans its own partial (see _download_one's BaseException handler).
    # We must AWAIT that cancellation cleanup before re-raising: bare
    # asyncio.gather propagates the first error while sibling cleanup is still
    # pending, leaking 0-byte/partial files. So on any failure we cancel every
    # task and await them all (suppressing their CancelledError) first.
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    total = len(selected)
    progress_lock = asyncio.Lock()
    done = 0

    async def _guarded(f: FileEntry) -> _Outcome:
        nonlocal done
        async with sem:
            outcome = await _download_one(
                client, f, target, budget=budget, force=force, extract=extract
            )
        if on_progress is not None:
            # Serialize the counter bump AND the emit so callbacks fire in
            # counter order — otherwise a slow callback for file N can be
            # overtaken by N+1 and the client sees ``done`` go backwards.
            async with progress_lock:
                done += 1
                await on_progress(done, total, outcome.name)
        return outcome

    tasks = [asyncio.create_task(_guarded(f)) for f in selected]
    try:
        outcomes: list[_Outcome] = list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        # Drain so every task's partial-cleanup finishes before we propagate.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    paths: list[str] = []
    skipped: list[str] = []
    resumed: list[str] = []
    written_total = 0
    for o in outcomes:
        if o.state == "skipped":
            skipped.append(o.name)
            continue
        if o.state == "resumed":
            resumed.append(o.name)
        if o.path is not None:
            paths.append(o.path)
            paths.extend(o.extracted)
        written_total += o.bytes

    # Sort for order-stability: completion order under gather is nondeterministic.
    paths.sort()
    skipped.sort()
    resumed.sort()

    (target / ".dataresource.json").write_text(resource.model_dump_json(indent=2))
    return FetchResult(paths=paths, bytes=written_total, skipped=skipped, resumed=resumed)
