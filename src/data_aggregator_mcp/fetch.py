"""Stream files to a content-addressed disk cache. Returns paths, never bytes."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from data_aggregator_mcp import archive
from data_aggregator_mcp.errors import FetchTooLargeError, NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, FetchResult, FileEntry

DEFAULT_MAX_BYTES = 2_000_000_000  # ~2 GB
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "data-aggregator-mcp"
_CHUNK = 1 << 16  # 64 KiB

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
        out.unlink(missing_ok=True)
        sc = exc.response.status_code
        if sc == 404:
            raise NotFoundError(f"fetch {f.name} → HTTP 404 ({f.url})") from exc
        raise UpstreamUnavailableError(f"fetch {f.name} → HTTP {sc} ({f.url})") from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        out.unlink(missing_ok=True)
        raise UpstreamUnavailableError(f"fetch {f.name} transport failure: {exc!r}") from exc
    except FetchTooLargeError:
        out.unlink(missing_ok=True)
        raise
    if h is not None and f.checksum:
        expected = f.checksum.split(":", 1)[1]
        if h.hexdigest() != expected:
            out.unlink(missing_ok=True)
            raise UpstreamUnavailableError(f"checksum mismatch for {f.name}")
    if h is None and f.mime in _BINARY_MIMES and _looks_like_html(first_head):
        out.unlink(missing_ok=True)
        raise UpstreamUnavailableError(
            f"fetch {f.name}: body is HTML, not the declared {f.mime} "
            "(the URL likely served a login/paywall/error page)"
        )
    extracted: list[str] = []
    if extract and archive.is_archive(safe_name):
        members = archive.extract_archive(out, target, max_bytes=budget.cap)
        extracted = [str(m) for m in members]
    return _Outcome(f.name, path=str(out), extracted=extracted, bytes=written, state="downloaded")


async def fetch_files(
    client: httpx.AsyncClient,
    resource: DataResource,
    *,
    dest: str | None = None,
    files: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    force: bool = False,
    extract: bool = False,
) -> FetchResult:
    """Download ``resource`` files to disk. ``files`` is an optional glob over
    file names. Fails loud over ``max_bytes`` unless ``force``. Verifies
    checksums when present; writes a ``.dataresource.json`` provenance sidecar.
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

    outcomes: list[_Outcome] = []
    for f in selected:
        outcomes.append(
            await _download_one(client, f, target, budget=budget, force=force, extract=extract)
        )

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

    (target / ".dataresource.json").write_text(resource.model_dump_json(indent=2))
    return FetchResult(paths=paths, bytes=written_total, skipped=skipped, resumed=resumed)
