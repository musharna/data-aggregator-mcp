# src/data_aggregator_mcp/duckquery.py
"""Hardened DuckDB+httpfs engine for operate's head/sql ops.

The remote source is read eagerly into an in-memory table named ``data``; all external
access (every filesystem, local or remote) is then disabled and the configuration
locked, so a user SELECT cannot read local files, reach the network, write, or
re-enable anything.
Only a single query statement, as DuckDB parses it, is accepted. Sync calls run in
``asyncio.to_thread``; the whole call is wall-clock-bounded by the caller, and when the
caller gives up, the connection is interrupted until its thread ends (see
``_interruptible``).

Security posture (see ``_connect``): the source is materialized via ``CREATE
TABLE`` while the filesystems are still enabled, then we ``SET
enable_external_access=false`` and ``SET lock_configuration=true``. After that point
a crafted ``read_csv_auto('/etc/passwd')`` — or ``'http://169.254.169.254/...'``,
``'s3://...?s3_endpoint=<host>'``, ``'hf://...'`` — raises a ``PermissionException``
instead of returning content, and the user statement cannot re-enable access or
change config (the lock is sticky). Access is switched off wholesale rather than by
naming filesystems: httpfs registers HTTP, S3 (s3/s3a/s3n/r2/gcs) and HuggingFace
filesystems, and a denylist of two left the rest open.
Disabling remote access matters as much as disabling local: with httpfs left loaded, a user
SELECT made the SERVER fetch an arbitrary URL and returned the body as rows. That
is invisible over stdio (the server is the caller's own child, same network
position) but is SSRF with response exfiltration under ``--transport http``, where
the server may sit in a network the caller cannot otherwise reach. Eager
materialization is REQUIRED: DuckDB evaluates a ``CREATE VIEW`` lazily at query
time, i.e. AFTER the lock, which would also block the legitimate source read —
both ``file://`` and bare-path local reads route through ``LocalFileSystem``, so
there is no view-based way to keep the legit read working while the FS is
disabled. ``CREATE TABLE`` does the source read up front, before the lock, and it
reads a local file: a remote source is downloaded first through ``sourceio``, whose
client checks every redirect hop against the egress guard, which httpfs cannot do.
Cost: the source is fully loaded into RAM at connect time, which the caller
bounds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from data_aggregator_mcp import sourceio
from data_aggregator_mcp.errors import ValidationError

_PARQUET_EXTS = (".parquet", ".pq")
DEFAULT_ROW_CAP = 1000

# The SQL the engine runs itself. Module constants, so the case-only rewrites a mutation
# run makes of an inline literal (SQL keywords and names are case-insensitive) cannot
# arise; the tests that drive each statement are named beside it.
_PARQUET_READER = "read_parquet"  # test_peek_format_parity_parquet_vs_csv
_CSV_READER = "read_csv_auto"  # test_peek_format_parity_parquet_vs_csv
_LOAD_HTTPFS = "INSTALL httpfs; LOAD httpfs;"  # test_user_sql_cannot_reach_the_network
# test_local_file_read_rejected, test_user_sql_cannot_reach_the_network_via_any_remote_filesystem
_LOCKDOWN = ("SET enable_external_access=false;", "SET lock_configuration=true;")
_SUMMARIZE = "SUMMARIZE data"  # test_peek_parquet_profile_shape_and_null_rate
_ROW_COUNT = "SELECT COUNT(*) FROM data"  # test_peek_profiles_a_file_with_a_header_and_no_rows
# How often a timed-out query's connection is interrupted until its thread ends.
_INTERRUPT_EVERY_S = 0.05
# The tasks still stopping a timed-out query; held here so none is garbage-collected
# before its thread has ended.
_STOPPING: set[asyncio.Task] = set()


def _reader(url: str, file: str) -> str:
    fn = _PARQUET_READER if file.lower().endswith(_PARQUET_EXTS) else _CSV_READER
    safe = url.replace("'", "''")
    return f"{fn}('{safe}')"


async def _interruptible(fn: Callable[..., dict], *args: Any) -> dict:
    """Run ``fn(*args, opened)`` in a worker thread; ``fn`` passes ``opened`` each
    connection it opens.

    operate's wall-clock limit cancels the await, and a cancelled await does not stop the
    thread it was waiting on: the query ran to its end after the timeout was
    reported. So on cancellation the connection is interrupted until the thread ends. A
    separate task does that, and the cancellation is not held up for it: an interrupt does
    not stop a source download in progress, only the work after it, so waiting here would
    stretch the limit to the length of the download.
    """
    cons: list[Any] = []
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, cons.append))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_discard_outcome)
        stopping = asyncio.ensure_future(_stop(task, cons))
        _STOPPING.add(stopping)
        stopping.add_done_callback(_STOPPING.discard)
        raise


def _discard_outcome(task: asyncio.Future) -> None:
    # Nobody waits for this answer any more (the caller was told it timed out); taking
    # the outcome keeps asyncio from reporting the interrupt as an unretrieved error.
    if not task.cancelled():
        task.exception()


async def _stop(task: asyncio.Future, cons: list[Any]) -> None:
    while not task.done():
        _interrupt(cons)
        await asyncio.wait({task}, timeout=_INTERRUPT_EVERY_S)


def _interrupt(cons: list[Any]) -> None:
    import duckdb

    for con in cons:
        try:
            con.interrupt()
        except duckdb.ConnectionException:
            # The thread closed it between two interrupts: its work is over.
            continue


def _validate_select(query: str) -> str:
    """Refuse anything but one query statement, as DuckDB's own parser reads it.

    A text check could not tell a ``;`` between statements from one inside a string
    (``WHERE go = 'GO:1;GO:2'`` was refused), and its prefix regex refused DuckDB's own
    query forms (``FROM data``, a leading comment). Malformed SQL raises DuckDB's
    ``ParserException`` here, before the source is downloaded.
    """
    import duckdb

    statements = duckdb.extract_statements(query)
    if len(statements) > 1:
        raise ValidationError("operate sql accepts a single statement only")
    if not statements or statements[0].type != duckdb.StatementType.SELECT:
        raise ValidationError("operate sql accepts a read-only SELECT/WITH query only")
    return query


def _connect(url: str, file: str, opened: Callable[[Any], None]):
    import duckdb

    con = duckdb.connect(database=":memory:")
    opened(con)  # before the first statement, so every statement can be interrupted
    con.execute(_LOAD_HTTPFS)
    # Eager read FIRST (both filesystems still enabled), then lock them down. See the
    # module docstring: a CREATE VIEW would be evaluated lazily after the lock and
    # would block the legit source read too, so we materialize a TABLE here.
    # DuckDB never sees a remote URL: httpfs follows redirects with no egress check, so a
    # public record URL that 302s into private space had its body read. sourceio
    # downloads it through the guarded client and DuckDB reads the local copy.
    with sourceio.local_copy(url) as local:
        con.execute(f"CREATE TABLE data AS SELECT * FROM {_reader(local, file)};")  # nosec B608 - url is ''-escaped in _reader
    # The source is in memory by this line, so nothing downstream needs any file or
    # network access: turn ALL of it off. Naming filesystems to disable was a denylist
    # that httpfs outgrows — it also registers S3 (s3/s3a/s3n/r2/gcs) and HuggingFace
    # (hf://) filesystems, and with only Local+HTTP disabled a user SELECT read public
    # buckets and, via a per-URL s3_endpoint, connected to any host.
    for statement in _LOCKDOWN:
        con.execute(statement)
    return con


def _run(url: str, file: str, sql: str, row_cap: int, opened: Callable[[Any], None]) -> dict:
    con = _connect(url, file, opened)
    try:
        # The cap is put on the parsed query, not appended to its text: a trailing
        # ``--`` in the user's SQL commented out an appended ``) LIMIT n``, so a query
        # ending ``... ) --`` fetched every row of a cross join into Python.
        rel = con.sql(sql).limit(row_cap + 1)
        cols = [{"name": d[0], "type": str(d[1])} for d in rel.description]
        rows = rel.fetchall()
    finally:
        con.close()
    truncated = len(rows) > row_cap
    rows = rows[:row_cap]
    return {
        "columns": cols,
        "rows": _records([c["name"] for c in cols], rows),
        "truncated": truncated,
    }


def _records(names: list[str], rows: list[tuple]) -> list[dict]:
    # Each row is zipped with the column names of its own result, so the lengths always
    # match and strict=True/False/None behave alike; test_sql_rows_and_columns_are_mapped_exactly
    # and test_peek_profile_is_mapped_field_by_field pin the mapping.
    return [dict(zip(names, r, strict=True)) for r in rows]  # pragma: no mutate


async def run_sql(url: str, file: str, query: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    sql = _validate_select(query)
    return await _interruptible(_run, url, file, sql, row_cap)


async def run_head(url: str, file: str, *, n: int, columns: list[str] | None) -> dict:
    proj = ", ".join('"' + c.replace('"', '""') + '"' for c in columns) if columns else "*"
    return await _interruptible(_run, url, file, f"SELECT {proj} FROM data", n)  # nosec B608 - proj is '"'-quoted


def _normalize_summary_row(d: dict) -> dict:
    """Map one DuckDB ``SUMMARIZE`` row to a JSON-safe, honestly-named column profile.

    SUMMARIZE hands back ``column_name, column_type, min, max, approx_unique, avg, std,
    q25, q50, q75, count, null_percentage``. We surface a normalized subset:

    - ``null_percentage`` is coerced to a real ``float`` (DuckDB returns a Decimal), and
      is ``None`` for a file with no rows (DuckDB answers NULL: there is no percentage).
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
        "null_percentage": None if d["null_percentage"] is None else float(d["null_percentage"]),
        "approx_unique": None if d["approx_unique"] is None else int(d["approx_unique"]),
        "min": _s(d["min"]),
        "max": _s(d["max"]),
        "avg": _s(d["avg"]),
        "std": _s(d["std"]),
        "q25": _s(d["q25"]),
        "q50": _s(d["q50"]),
        "q75": _s(d["q75"]),
    }


def _peek(url: str, file: str, opened: Callable[[Any], None]) -> dict:
    con = _connect(url, file, opened)  # REUSE the hardened lockdown engine (no FS re-enable)
    try:
        rel = con.execute(_SUMMARIZE)
        raw = _records([d[0] for d in rel.description], rel.fetchall())
        row_count = con.execute(_ROW_COUNT).fetchone()[0]
    finally:
        con.close()
    profile = [_normalize_summary_row(d) for d in raw]
    return {"row_count": int(row_count), "columns": profile}


async def run_peek(url: str, file: str) -> dict:
    return await _interruptible(_peek, url, file)
