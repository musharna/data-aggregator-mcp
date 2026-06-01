# tests/test_duckquery.py
import pathlib

import pytest

from data_aggregator_mcp import duckquery
from data_aggregator_mcp.errors import ValidationError

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()


@pytest.mark.asyncio
async def test_sql_filters_rows():
    out = await duckquery.run_sql(
        PARQUET_URL, "sample.parquet", "SELECT name FROM data WHERE temp > 30"
    )
    assert {r["name"] for r in out["rows"]} == {"b", "c"}
    assert out["columns"][0]["name"] == "name"


@pytest.mark.asyncio
async def test_head_limits_rows():
    out = await duckquery.run_head(PARQUET_URL, "sample.parquet", n=2, columns=None)
    assert len(out["rows"]) == 2


@pytest.mark.asyncio
async def test_row_cap_marks_truncated():
    out = await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT * FROM data", row_cap=2)
    assert len(out["rows"]) == 2 and out["truncated"] is True


@pytest.mark.asyncio
async def test_non_select_rejected():
    with pytest.raises(ValidationError):
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", "DROP TABLE data")


@pytest.mark.asyncio
async def test_local_file_read_rejected():
    # A query reaching outside the registered view into the local FS must fail loud,
    # NOT return /etc/passwd contents. DuckDB's PermissionException is not one of our
    # typed errors, so we catch broadly but then POSITIVELY require evidence that the
    # SET disabled_filesystems='LocalFileSystem' hardening (not just "any error") fired.
    with pytest.raises(Exception) as ei:
        await duckquery.run_sql(
            PARQUET_URL, "sample.parquet", "SELECT * FROM read_csv_auto('/etc/passwd')"
        )
    msg = str(ei.value).lower()
    # Block-evidence substring (AND with the leak-check) proves the FS-disabled hardening
    # fired, not merely that "an error happened".
    assert "disabled" in msg or "localfilesystem" in msg or "permission" in msg
    assert "root:" not in msg  # never leak passwd contents into the error


@pytest.mark.asyncio
async def test_write_copy_rejected():
    # COPY is not a SELECT, so the SELECT-only validation must reject it before execution.
    with pytest.raises(ValidationError):
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", "COPY data TO '/tmp/x.csv'")
