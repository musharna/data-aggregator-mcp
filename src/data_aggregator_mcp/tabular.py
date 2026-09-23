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


def _delimiter(name: str) -> str:
    """The field separator a delimited file's NAME declares. ``.tsv`` read with the
    comma dialect came back as one column holding the whole tab-joined header."""
    return "\t" if name.lower().endswith(".tsv") else ","


def _format(name: str) -> str:
    return "tsv" if _delimiter(name) == "\t" else "csv"


def _read_head_text(url: str, n: int) -> tuple[str, bool]:
    """Up to ``n`` bytes from the start of ``url`` as text, plus whether the file
    continues past them. When it does, the trailing partial line is dropped so no
    half-row is ever parsed as data; if the window holds no newline at all (a header
    wider than ``n``) the raw window is kept and the cut is still reported."""
    with fsspec.open(url, "rb") as f:
        raw = f.read(n + 1)
    capped = len(raw) > n
    if capped:
        raw = raw[:n]
        nl = raw.rfind(b"\n")
        if nl >= 0:
            raw = raw[: nl + 1]
    return raw.decode("utf-8", "replace"), capped


def _schema_csv(url: str, file: str) -> dict:
    head, capped = _read_head_text(url, _CSV_SNIFF_BYTES)
    reader = csv.reader(io.StringIO(head), delimiter=_delimiter(file))
    header = next(reader, [])
    out: dict = {
        "format": _format(file),
        "columns": [{"name": h, "type": "string"} for h in header],
        "row_estimate": None,
    }
    if capped and "\n" not in head:
        out["truncated"] = True  # the header itself is wider than the sniff window
    return out


async def schema(url: str, file: str) -> dict:
    if _is_parquet(file):
        return await asyncio.to_thread(_schema_parquet, url)
    return await asyncio.to_thread(_schema_csv, url, file)


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


def _preview_csv(url: str, file: str, n: int) -> dict:
    head, capped = _read_head_text(url, _CSV_SNIFF_BYTES)
    reader = csv.DictReader(io.StringIO(head), delimiter=_delimiter(file))
    rows = []
    for i, row in enumerate(reader):
        if i >= n:
            break
        rows.append(dict(row))
    cols = [{"name": h, "type": "string"} for h in (reader.fieldnames or [])]
    out: dict = {"format": _format(file), "columns": cols, "rows": rows, "row_estimate": None}
    if capped and (len(rows) < n or "\n" not in head):
        # The sniff window ran out before `n` rows: say so rather than let a short
        # page read as "the file has only this many rows".
        out["truncated"] = True
    return out


async def preview(url: str, file: str, *, n: int = 20) -> dict:
    if _is_parquet(file):
        return await asyncio.to_thread(_preview_parquet, url, n)
    return await asyncio.to_thread(_preview_csv, url, file, n)
