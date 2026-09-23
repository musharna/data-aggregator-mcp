# tests/test_tabular.py
import pathlib

import pytest

# These tests need the [operate] extra (pyarrow/fsspec). Skip cleanly in a
# base-only env instead of crashing at collection on the module-level imports.
pytest.importorskip("fsspec")
pytest.importorskip("pyarrow")

from data_aggregator_mcp import tabular

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()
CSV_URL = (FX / "sample.csv").as_uri()


@pytest.mark.asyncio
async def test_schema_parquet():
    out = await tabular.schema(PARQUET_URL, "sample.parquet")
    assert out["format"] == "parquet"
    assert {c["name"] for c in out["columns"]} == {"id", "name", "temp"}


@pytest.mark.asyncio
async def test_schema_csv():
    out = await tabular.schema(CSV_URL, "sample.csv")
    assert out["format"] == "csv"
    assert [c["name"] for c in out["columns"]] == ["id", "name", "temp"]


@pytest.mark.asyncio
async def test_preview_parquet_returns_rows():
    out = await tabular.preview(PARQUET_URL, "sample.parquet", n=2)
    assert len(out["rows"]) == 2
    assert out["rows"][0]["name"] == "a"


@pytest.mark.asyncio
async def test_preview_empty_parquet_is_empty_not_error(tmp_path):
    # A zero-row Parquet yields no batches; preview must return [] rows, not raise
    # (next() without a default would surface as an asyncio thread-result error).
    import pyarrow as pa
    import pyarrow.parquet as pq

    empty = pa.table({"id": pa.array([], type=pa.int64()), "name": pa.array([], type=pa.string())})
    p = tmp_path / "empty.parquet"
    pq.write_table(empty, p)

    out = await tabular.preview(p.as_uri(), "empty.parquet", n=5)
    assert out["rows"] == []
    assert [c["name"] for c in out["columns"]] == ["id", "name"]
    assert out["row_estimate"] == 0


@pytest.mark.asyncio
async def test_tsv_schema_and_preview_split_on_tabs(tmp_path):
    """M16 (audit 2026-09-22): a .tsv was parsed with the comma dialect, so the whole
    header came back as ONE column named 'gene\\tsample\\tcount'. The delimiter must
    follow the file type on every sniff path. Positive control: .csv still splits on
    commas."""
    tsv = tmp_path / "t.tsv"
    tsv.write_text("gene\tsample\tcount\nA\ts1\t5\nB\ts2\t7\n")
    sch = await tabular.schema(tsv.as_uri(), "t.tsv")
    assert [c["name"] for c in sch["columns"]] == ["gene", "sample", "count"]
    assert sch["format"] == "tsv"
    pre = await tabular.preview(tsv.as_uri(), "t.tsv", n=2)
    assert pre["rows"] == [
        {"gene": "A", "sample": "s1", "count": "5"},
        {"gene": "B", "sample": "s2", "count": "7"},
    ]
    csv_out = await tabular.schema(CSV_URL, "sample.csv")
    assert [c["name"] for c in csv_out["columns"]] == ["id", "name", "temp"]
    assert csv_out["format"] == "csv"


@pytest.mark.asyncio
async def test_csv_preview_cut_by_the_sniff_window_says_so(tmp_path):
    """L17 (audit 2026-09-22): preview reads a fixed 64 KB head. When the requested rows
    do not fit, it returned fewer rows — the last one cut mid-line and presented as real
    data — with no `truncated` flag. A partial trailing line is dropped and the cut is
    flagged. Positive control: a small file is complete and NOT flagged."""
    big = tmp_path / "wide.csv"
    cell = "x" * 100
    big.write_text("id,text,value\n" + "".join(f"{i},{cell},{i * 1000}\n" for i in range(2000)))
    out = await tabular.preview(big.as_uri(), "wide.csv", n=1000)
    assert out.get("truncated") is True
    assert 0 < len(out["rows"]) < 1000
    last = out["rows"][-1]
    # every returned row is a whole row: value == id * 1000, text intact
    assert last["text"] == cell and last["value"] == str(int(last["id"]) * 1000)
    small = await tabular.preview(CSV_URL, "sample.csv", n=2)
    assert not small.get("truncated")
    assert len(small["rows"]) == 2
