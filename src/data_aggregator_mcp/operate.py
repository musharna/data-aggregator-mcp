# src/data_aggregator_mcp/operate.py
"""The `operate` tool: inspect/query a remote tabular file without downloading it."""

from __future__ import annotations

import asyncio

import httpx

from data_aggregator_mcp import router
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import FileEntry

ROW_CAP = 1000
RESULT_BYTE_CAP = 5_000_000
WALL_TIMEOUT_S = 30.0
SOURCE_BYTE_CEILING = (
    100_000_000  # head/sql eager-load the whole file into RAM; refuse larger sources
)

MISSING_EXTRA_MSG = (
    "operate-on-data needs the optional extra: install `data-aggregator-mcp[operate]` "
    "(adds duckdb + pyarrow + fsspec)."
)

try:  # the heavy deps are import-guarded so the base install stays light
    import duckdb  # noqa: F401
    import pyarrow  # noqa: F401

    OPERATE_AVAILABLE = True
except ImportError:
    OPERATE_AVAILABLE = False

TABULAR_EXTS = (".parquet", ".pq", ".csv", ".tsv")
OPERATE_MODES = ("schema", "preview", "head", "sql")


def _operable(f: FileEntry) -> bool:
    return bool(f.url) and (f.name or "").lower().endswith(TABULAR_EXTS)


def _select_file(files: list[FileEntry], requested: str | None) -> FileEntry:
    operable = [f for f in files if _operable(f)]
    if requested is not None:
        for f in files:
            if f.name == requested:
                if not _operable(f):
                    raise OperateNotSupportedError(
                        f"file {requested!r} is not an operable tabular file (need {TABULAR_EXTS})"
                    )
                return f
        raise OperateNotSupportedError(f"file {requested!r} not found in record")
    if len(operable) == 1:
        return operable[0]
    if not operable:
        raise OperateNotSupportedError(
            "no operable tabular file in this record; resolve it and fetch instead"
        )
    raise OperateNotSupportedError(
        "record has multiple operable files; pass file=<name> — options: "
        + ", ".join(f.name for f in operable)
    )


def _source_size(url: str) -> int | None:
    """Best-effort source byte size (HEAD for http, stat for file://). Returns None
    when the server exposes no content-length. A real I/O error propagates (fail loud)."""
    import fsspec

    fs, _, paths = fsspec.core.get_fs_token_paths(url)
    size = fs.info(paths[0]).get("size")
    return int(size) if size is not None else None


def _cap_result_bytes(result: dict) -> dict:
    """Trim a rows-bearing result so its JSON payload stays under RESULT_BYTE_CAP,
    setting ``truncated`` when rows are dropped. The row cap bounds count; this
    bounds total bytes (1000 wide rows can still be large)."""
    import json

    rows = result.get("rows")
    if not rows:
        return result
    kept: list = []
    total = 0
    capped = False
    for r in rows:
        total += len(json.dumps(r, default=str).encode())
        if total > RESULT_BYTE_CAP:
            capped = True
            break
        kept.append(r)
    if capped:
        result["rows"] = kept
        result["truncated"] = True
    return result


async def run(
    client: httpx.AsyncClient,
    resource_id: str,
    op: str,
    *,
    file: str | None = None,
    query: str | None = None,
    n: int = 20,
    columns: list[str] | None = None,
) -> dict:
    if not OPERATE_AVAILABLE:
        raise OperateNotSupportedError(MISSING_EXTRA_MSG)
    if op not in OPERATE_MODES:
        raise ValidationError(f"unknown op {op!r}; expected one of {OPERATE_MODES}")
    if op == "sql" and not query:
        raise ValidationError("op='sql' requires a query")

    from data_aggregator_mcp import duckquery, tabular

    resource = await router.resolve(client, resource_id)
    target = _select_file(resource.files, file)
    n = min(n, ROW_CAP)

    # head/sql eager-load the whole remote file into memory (DuckDB materializes it
    # before locking down the local FS), so guard the source size. schema/preview use
    # the footer/CSV-sniff (range reads only) and are intentionally NOT gated.
    if op in ("head", "sql"):
        size = await asyncio.to_thread(_source_size, target.url)
        if size is not None and size > SOURCE_BYTE_CEILING:
            raise OperateNotSupportedError(
                f"{target.name!r} is {size} bytes, over the operate ceiling of "
                f"{SOURCE_BYTE_CEILING}; head/sql load the whole file into memory — use fetch instead"
            )

    async def _go() -> dict:
        if op == "schema":
            return await tabular.schema(target.url, target.name)
        if op == "preview":
            return await tabular.preview(target.url, target.name, n=n)
        if op == "head":
            return await duckquery.run_head(target.url, target.name, n=n, columns=columns)
        return await duckquery.run_sql(target.url, target.name, query, row_cap=ROW_CAP)

    try:
        result = await asyncio.wait_for(_go(), timeout=WALL_TIMEOUT_S)
    except TimeoutError as exc:
        raise OperateNotSupportedError(
            f"operate op={op!r} exceeded {WALL_TIMEOUT_S}s wall-clock limit"
        ) from exc
    result = _cap_result_bytes(result)
    result["file"] = target.name
    result["op"] = op
    return result
