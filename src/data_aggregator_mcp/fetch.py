"""Stream files to a content-addressed disk cache. Returns paths, never bytes."""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

import httpx

from data_aggregator_mcp import archive
from data_aggregator_mcp.errors import FetchTooLargeError, NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, FetchResult

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

    paths: list[str] = []
    skipped: list[str] = []
    written_total = 0
    for f in selected:
        if not f.url:
            skipped.append(f.name)
            continue
        # Sanitize: f.name is uploader-controlled (Zenodo file key). Reduce to a
        # bare basename so it cannot escape the target dir via path traversal.
        safe_name = Path(f.name).name
        if not safe_name or safe_name in (".", ".."):
            skipped.append(f.name)
            continue
        out = target / safe_name
        h = _hasher(f.checksum)
        written = 0
        first_head = b""
        first_chunk_seen = False
        try:
            async with client.stream("GET", f.url, timeout=300.0) as resp:
                resp.raise_for_status()
                with out.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        written += len(chunk)
                        if written_total + written > max_bytes and not force:
                            fh.close()
                            out.unlink(missing_ok=True)
                            raise FetchTooLargeError(
                                f"stream exceeded max_bytes={max_bytes} while fetching {f.name}"
                            )
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
        written_total += written
        paths.append(str(out))
        if extract and archive.is_archive(safe_name):
            members = archive.extract_archive(out, target, max_bytes=max_bytes)
            paths.extend(str(m) for m in members)

    (target / ".dataresource.json").write_text(resource.model_dump_json(indent=2))
    return FetchResult(paths=paths, bytes=written_total, skipped=skipped)
