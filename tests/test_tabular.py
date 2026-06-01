# tests/test_tabular.py
import pathlib

import pytest

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
