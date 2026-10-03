# src/data_aggregator_mcp/tabular.py
"""Cheap schema/preview for remote tabular files via the Parquet footer or a CSV
sniff — range reads only, no full scan. Sync libs run in asyncio.to_thread.

Every read goes through ``sourceio.open_source``, never fsspec's HTTP filesystem: that
follows redirects without the egress check, so a public URL could 302 into private space."""

from __future__ import annotations

import asyncio
import csv
import io
import itertools

import pyarrow.parquet as pq

from data_aggregator_mcp import sourceio

_PARQUET_EXTS = (".parquet", ".pq")
_CSV_SNIFF_BYTES = 64_000
# A leading UTF-8 byte-order mark (Excel's "CSV UTF-8" writes one) is not part of the
# first column's name: DuckDB drops it, so head/sql and schema/preview must agree.
_TEXT_ENCODING = "utf-8-sig"
_CUT_MARK = "_"  # see _preview_csv; any character but a quote, delimiter or line break


def _is_parquet(name: str) -> bool:
    return name.lower().endswith(_PARQUET_EXTS)


def _parquet_columns(pf: pq.ParquetFile) -> list[dict]:
    return [{"name": field.name, "type": str(field.type)} for field in pf.schema_arrow]


def _schema_parquet(url: str) -> dict:
    with sourceio.open_source(url) as f:
        pf = pq.ParquetFile(f)
        # An opened ParquetFile always has its footer metadata; the row count is exact.
        return {
            "format": "parquet",
            "columns": _parquet_columns(pf),
            "row_estimate": pf.metadata.num_rows,
        }


def _delimiter(name: str) -> str:
    """The field separator a delimited file's NAME declares. ``.tsv`` read with the
    comma dialect came back as one column holding the whole tab-joined header."""
    return "\t" if name.lower().endswith(".tsv") else ","


def _format(name: str) -> str:
    return "tsv" if _delimiter(name) == "\t" else "csv"


def _read_head_text(url: str, n: int) -> tuple[str, bool]:
    """Up to ``n`` bytes from the start of ``url`` as text, plus whether the file
    continues past them (one byte more is read to tell). A cut window ends anywhere,
    even inside a character: ``_preview_csv`` drops its last record, and a header
    with no line break in the window is reported cut by both readers."""
    with sourceio.open_source(url) as f:
        raw = f.read(n + 1)
    return raw[:n].decode(_TEXT_ENCODING, "replace"), len(raw) > n


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
    with sourceio.open_source(url) as f:
        pf = pq.ParquetFile(f)
        # An empty Parquet (zero row groups) yields no batches; next() must not raise
        # StopIteration here — asyncio rejects it as a thread-result exception.
        batch = next(pf.iter_batches(batch_size=n), None)
        rows = batch.to_pylist() if batch is not None else []
        return {
            "format": "parquet",
            "columns": _parquet_columns(pf),
            "rows": rows[:n],
            "row_estimate": pf.metadata.num_rows,
        }


def _preview_csv(url: str, file: str, n: int) -> dict:
    head, capped = _read_head_text(url, _CSV_SNIFF_BYTES)
    # A cut window's last record is half a row: cut mid-line, or at a line break inside
    # a quoted field, which does not end a row. A mark appended after the cut becomes a
    # record of its own when the window happens to end a row, joins the half row when
    # it does not: either way the last record is no row. Without a line break the
    # window is all header, and the mark would join a column name.
    cut = capped and "\n" in head
    text = head + _CUT_MARK if cut else head
    reader = csv.DictReader(io.StringIO(text), delimiter=_delimiter(file))
    rows = [dict(row) for row in itertools.islice(reader, n)]
    if cut and next(reader, None) is None:
        rows = rows[:-1]
    cols = [{"name": h, "type": "string"} for h in (reader.fieldnames or [])]
    out: dict = {"format": _format(file), "columns": cols, "rows": rows, "row_estimate": None}
    if capped and len(rows) < n:
        # The sniff window ran out before `n` rows: say so rather than let a short
        # page read as "the file has only this many rows".
        out["truncated"] = True
    return out


async def preview(url: str, file: str, *, n: int) -> dict:
    if _is_parquet(file):
        return await asyncio.to_thread(_preview_parquet, url, n)
    return await asyncio.to_thread(_preview_csv, url, file, n)
