# src/data_aggregator_mcp/duckquery.py
"""Hardened DuckDB+httpfs engine for operate's head/sql ops.

The remote source is read eagerly into an in-memory table named ``data``; the
local filesystem is then disabled and the configuration locked, so a user SELECT
cannot read local files, write, or re-enable anything. Only a single SELECT/WITH
statement is accepted. Sync calls run in ``asyncio.to_thread``; the whole call is
wall-clock-bounded by the caller.

Security posture (see ``_connect``): the source is materialized via ``CREATE
TABLE`` while the local FS is still enabled, then we ``SET
disabled_filesystems='LocalFileSystem'`` and ``SET lock_configuration=true``.
After that point a crafted ``read_csv_auto('/etc/passwd')`` raises a
``PermissionException`` instead of returning file contents, and the user
statement cannot re-enable the FS or change config (the lock is sticky). Eager
materialization is REQUIRED: DuckDB evaluates a ``CREATE VIEW`` lazily at query
time, i.e. AFTER the lock, which would also block the legitimate source read —
both ``file://`` and bare-path local reads route through ``LocalFileSystem``, so
there is no view-based way to keep the legit read working while the FS is
disabled. ``CREATE TABLE`` does the source read up front; httpfs http(s) range
reads in production likewise complete during that eager read, before the lock.
Cost: the source is fully loaded into RAM at connect time, which the caller
bounds.
"""

from __future__ import annotations

import asyncio
import re

from data_aggregator_mcp.errors import ValidationError

_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_PARQUET_EXTS = (".parquet", ".pq")
DEFAULT_ROW_CAP = 1000


def _reader(url: str, file: str) -> str:
    fn = "read_parquet" if file.lower().endswith(_PARQUET_EXTS) else "read_csv_auto"
    safe = url.replace("'", "''")
    return f"{fn}('{safe}')"


def _validate_select(query: str) -> str:
    q = query.strip().rstrip(";")
    if ";" in q:
        raise ValidationError("operate sql accepts a single statement only")
    if not _SELECT_RE.match(q):
        raise ValidationError("operate sql accepts a read-only SELECT/WITH query only")
    return q


def _connect(url: str, file: str):
    import duckdb

    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # Eager read FIRST (local FS still enabled), then lock the FS down. See the
    # module docstring: a CREATE VIEW would be evaluated lazily after the lock and
    # would block the legit source read too, so we materialize a TABLE here.
    con.execute(f"CREATE TABLE data AS SELECT * FROM {_reader(url, file)};")
    con.execute("SET disabled_filesystems='LocalFileSystem';")
    con.execute("SET lock_configuration=true;")
    return con


def _run(url: str, file: str, sql: str, row_cap: int) -> dict:
    con = _connect(url, file)
    try:
        rel = con.execute(f"SELECT * FROM ({sql}) LIMIT {row_cap + 1}")
        cols = [{"name": d[0], "type": str(d[1])} for d in rel.description]
        rows = rel.fetchall()
    finally:
        con.close()
    truncated = len(rows) > row_cap
    rows = rows[:row_cap]
    names = [c["name"] for c in cols]
    return {
        "columns": cols,
        "rows": [dict(zip(names, r, strict=False)) for r in rows],
        "truncated": truncated,
    }


async def run_sql(url: str, file: str, query: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    sql = _validate_select(query)
    return await asyncio.to_thread(_run, url, file, sql, row_cap)


async def run_head(url: str, file: str, *, n: int, columns: list[str] | None) -> dict:
    proj = ", ".join('"' + c.replace('"', '""') + '"' for c in columns) if columns else "*"
    return await asyncio.to_thread(_run, url, file, f"SELECT {proj} FROM data", n)


def _normalize_summary_row(d: dict) -> dict:
    """Map one DuckDB ``SUMMARIZE`` row to a JSON-safe, honestly-named column profile.

    SUMMARIZE hands back ``column_name, column_type, min, max, approx_unique, avg, std,
    q25, q50, q75, count, null_percentage``. We surface a normalized subset:

    - ``null_percentage`` is coerced to a real ``float`` (DuckDB returns a Decimal).
    - ``approx_unique`` keeps its name — it is an APPROXIMATE distinct count (HyperLogLog),
      never an exact ``distinct``/``unique``.
    - ``min``/``max`` are stringified (column-type-dependent) for a uniform wire type;
      ``None`` stays ``None``.
    - ``avg``/``std``/``q25``/``q50``/``q75`` are present (as str) for numeric columns and
      ``None`` for text columns — a ``None`` honestly means "not applicable", never a
      fabricated ``0``.
    - the per-column ``count`` is OMITTED: SUMMARIZE ``count`` is the TOTAL row count (same
      for every column), NOT the non-null count, so surfacing it as "count" would mislead.
      The top-level ``row_count`` plus ``null_percentage`` already give non-null counts.
    """

    def _s(v: object) -> str | None:
        return None if v is None else str(v)

    return {
        "column_name": str(d["column_name"]),
        "column_type": str(d["column_type"]),
        "null_percentage": float(d["null_percentage"]),
        "approx_unique": None if d["approx_unique"] is None else int(d["approx_unique"]),
        "min": _s(d["min"]),
        "max": _s(d["max"]),
        "avg": _s(d["avg"]),
        "std": _s(d["std"]),
        "q25": _s(d["q25"]),
        "q50": _s(d["q50"]),
        "q75": _s(d["q75"]),
    }


def _peek(url: str, file: str) -> dict:
    con = _connect(url, file)  # REUSE the hardened lockdown engine (no FS re-enable)
    try:
        rel = con.execute("SUMMARIZE data")
        names = [d[0] for d in rel.description]
        raw = [dict(zip(names, r, strict=False)) for r in rel.fetchall()]
        row_count = con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
    finally:
        con.close()
    profile = [_normalize_summary_row(d) for d in raw]
    return {"row_count": int(row_count), "columns": profile}


async def run_peek(url: str, file: str) -> dict:
    return await asyncio.to_thread(_peek, url, file)
