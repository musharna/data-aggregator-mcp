# src/data_aggregator_mcp/tabular.py
"""Cheap schema/preview for remote tabular files via the Parquet footer or a CSV
sniff — range reads only, no full scan. Sync libs run in asyncio.to_thread."""

from __future__ import annotations

import asyncio
import csv
import io

import fsspec
import pyarrow.parquet as pq

_PARQUET_EXTS = (".parquet", ".pq")
_CSV_SNIFF_BYTES = 64_000


def _is_parquet(name: str) -> bool:
    return name.lower().endswith(_PARQUET_EXTS)


def _arrow_type(t) -> str:
    return str(t)


def _schema_parquet(url: str) -> dict:
    with fsspec.open(url, "rb") as f:
        pf = pq.ParquetFile(f)
        cols = [
            {"name": n, "type": _arrow_type(t)}
            for n, t in zip(pf.schema_arrow.names, pf.schema_arrow.types, strict=False)
        ]
        nrows = pf.metadata.num_rows if pf.metadata is not None else None
    return {"format": "parquet", "columns": cols, "row_estimate": nrows}


def _read_head_bytes(url: str, n: int) -> bytes:
    with fsspec.open(url, "rb") as f:
        return f.read(n)


def _schema_csv(url: str) -> dict:
    head = _read_head_bytes(url, _CSV_SNIFF_BYTES).decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(head))
    header = next(reader, [])
    return {
        "format": "csv",
        "columns": [{"name": h, "type": "string"} for h in header],
        "row_estimate": None,
    }


async def schema(url: str, file: str) -> dict:
    fn = _schema_parquet if _is_parquet(file) else _schema_csv
    return await asyncio.to_thread(fn, url)


def _preview_parquet(url: str, n: int) -> dict:
    with fsspec.open(url, "rb") as f:
        pf = pq.ParquetFile(f)
        cols = [
            {"name": cn, "type": _arrow_type(t)}
            for cn, t in zip(pf.schema_arrow.names, pf.schema_arrow.types, strict=False)
        ]
        nrows = pf.metadata.num_rows if pf.metadata is not None else None
        # An empty Parquet (zero row groups) yields no batches; next() must not raise
        # StopIteration here — asyncio rejects it as a thread-result exception.
        batch = next(pf.iter_batches(batch_size=n), None)
        rows = batch.to_pylist() if batch is not None else []
    return {"format": "parquet", "columns": cols, "rows": rows[:n], "row_estimate": nrows}


def _preview_csv(url: str, n: int) -> dict:
    head = _read_head_bytes(url, _CSV_SNIFF_BYTES).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(head))
    rows = []
    for i, row in enumerate(reader):
        if i >= n:
            break
        rows.append(dict(row))
    cols = [{"name": h, "type": "string"} for h in (reader.fieldnames or [])]
    return {"format": "csv", "columns": cols, "rows": rows, "row_estimate": None}


async def preview(url: str, file: str, *, n: int = 20) -> dict:
    fn = _preview_parquet if _is_parquet(file) else _preview_csv
    return await asyncio.to_thread(fn, url, n)
