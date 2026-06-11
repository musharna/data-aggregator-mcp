# tests/test_duckquery.py
import pathlib

import pytest

# Needs the [operate] extra (duckdb + pyarrow). Skip cleanly in a base-only env.
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import pyarrow as pa
import pyarrow.parquet as pq

from data_aggregator_mcp import duckquery
from data_aggregator_mcp.errors import ValidationError

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()

# A mixed-type fixture with a KNOWN null fraction: `name` is VARCHAR fully-populated
# (2 distinct of 3 rows), `temp` is numeric with exactly one null in three (~33.33%).
_TBL = pa.table(
    {
        "id": pa.array([1, 2, 3], type=pa.int64()),
        "name": pa.array(["a", "b", "a"], type=pa.string()),
        "temp": pa.array([29.5, None, 33.2], type=pa.float64()),
    }
)


@pytest.fixture
def peek_parquet(tmp_path):
    p = tmp_path / "peek.parquet"
    pq.write_table(_TBL, p)
    return p.as_uri(), "peek.parquet"


@pytest.fixture
def peek_csv(tmp_path):
    p = tmp_path / "peek.csv"
    # DuckDB's CSV reader treats an empty field as NULL, giving the same 1-in-3 null rate.
    p.write_text("id,name,temp\n1,a,29.5\n2,b,\n3,a,33.2\n")
    return p.as_uri(), "peek.csv"


def _by_name(profile):
    return {c["column_name"]: c for c in profile}


@pytest.mark.asyncio
async def test_peek_parquet_profile_shape_and_null_rate(peek_parquet):
    url, name = peek_parquet
    out = await duckquery.run_peek(url, name)
    assert out["row_count"] == 3
    cols = _by_name(out["columns"])
    assert set(cols) == {"id", "name", "temp"}
    # null_percentage is a COMPUTED float, not guessed: one null in three ~= 33.33.
    assert isinstance(cols["temp"]["null_percentage"], float)
    assert abs(cols["temp"]["null_percentage"] - 33.33) < 0.01
    # a fully-populated column is exactly 0.0.
    assert cols["name"]["null_percentage"] == 0.0


@pytest.mark.asyncio
async def test_peek_approx_unique_is_int_and_named(peek_parquet):
    url, name = peek_parquet
    out = await duckquery.run_peek(url, name)
    cols = _by_name(out["columns"])
    # the KEY is literally `approx_unique` (documents non-exactness), and it is an int.
    assert "approx_unique" in cols["name"]
    assert "distinct" not in cols["name"] and "unique" not in cols["name"]
    assert isinstance(cols["name"]["approx_unique"], int)
    assert cols["name"]["approx_unique"] == 2  # {a, b}
    assert cols["id"]["approx_unique"] == 3


@pytest.mark.asyncio
async def test_peek_text_numeric_stats_are_none_not_fabricated(peek_parquet):
    url, name = peek_parquet
    out = await duckquery.run_peek(url, name)
    cols = _by_name(out["columns"])
    # VARCHAR column: numeric stats are None (not 0/fabricated).
    for k in ("avg", "std", "q25", "q50", "q75"):
        assert cols["name"][k] is None, f"text column {k} must be None, got {cols['name'][k]!r}"
    # numeric column: present (as str).
    assert isinstance(cols["temp"]["avg"], str)
    assert isinstance(cols["temp"]["q50"], str)


@pytest.mark.asyncio
async def test_peek_omits_per_column_count(peek_parquet):
    url, name = peek_parquet
    out = await duckquery.run_peek(url, name)
    for c in out["columns"]:
        # SUMMARIZE `count` is total-not-non-null; we omit it to avoid the misread.
        assert "count" not in c


@pytest.mark.asyncio
async def test_peek_format_parity_parquet_vs_csv(peek_parquet, peek_csv):
    p_out = await duckquery.run_peek(*peek_parquet)
    c_out = await duckquery.run_peek(*peek_csv)
    assert p_out["row_count"] == c_out["row_count"] == 3

    def keyset(out):
        return {frozenset(c) for c in out["columns"]}

    # Parquet and CSV yield the SAME per-column key set (normalized across formats).
    assert keyset(p_out) == keyset(c_out)
    # and the CSV null-rate is computed the same way.
    c_cols = _by_name(c_out["columns"])
    assert abs(c_cols["temp"]["null_percentage"] - 33.33) < 0.01


@pytest.mark.asyncio
async def test_peek_does_not_reenable_local_fs():
    # peek routes through the hardened _connect; after a peek the local FS stays sealed.
    # peek takes no SQL, so we prove the seal via the same engine on a run_sql probe that
    # tries to reach /etc/passwd — it must be blocked, not leaked.
    out = await duckquery.run_peek(PARQUET_URL, "sample.parquet")
    assert out["row_count"] == 3
    with pytest.raises(Exception) as ei:
        await duckquery.run_sql(
            PARQUET_URL, "sample.parquet", "SELECT * FROM read_csv_auto('/etc/passwd')"
        )
    msg = str(ei.value).lower()
    assert "disabled" in msg or "localfilesystem" in msg or "permission" in msg
    assert "root:" not in msg


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


@pytest.mark.asyncio
async def test_head_column_quote_is_escaped():
    # a column name containing a double-quote must not break out of the identifier
    # quoting into injected SQL — it should be treated as a (nonexistent) column name
    # and raise a binder error, NOT execute injected statements.
    with pytest.raises(Exception) as ei:
        await duckquery.run_head(
            PARQUET_URL,
            "sample.parquet",
            n=2,
            columns=['name" ; DROP TABLE data; --'],
        )
    msg = str(ei.value).lower()
    # the doubled-quote kept the payload as a single (nonexistent) identifier, so
    # DuckDB raises a binder/column error — proving no statement-stacking break-out.
    assert "binder error" in msg or "not found" in msg or "does not have a column" in msg
