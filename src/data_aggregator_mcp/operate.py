# src/data_aggregator_mcp/operate.py
"""The `operate` tool: inspect/query a remote tabular file without downloading it."""

from __future__ import annotations

ROW_CAP = 1000
RESULT_BYTE_CAP = 5_000_000
WALL_TIMEOUT_S = 30.0
CSV_SOURCE_CEILING = 100_000_000  # CSV has no pushdown; refuse larger sources

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
