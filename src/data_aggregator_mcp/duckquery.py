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
        "rows": [dict(zip(names, r)) for r in rows],
        "truncated": truncated,
    }


async def run_sql(url: str, file: str, query: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    sql = _validate_select(query)
    return await asyncio.to_thread(_run, url, file, sql, row_cap)


async def run_head(url: str, file: str, *, n: int, columns: list[str] | None) -> dict:
    proj = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    return await asyncio.to_thread(_run, url, file, f"SELECT {proj} FROM data", n)
