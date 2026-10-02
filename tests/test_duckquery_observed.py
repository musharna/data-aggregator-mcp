# tests/test_duckquery_observed.py
"""What duckquery observably does: the statements it accepts and refuses, the rows it
fetches, and the exact shape of what it returns."""

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import duckdb

from data_aggregator_mcp import duckquery
from data_aggregator_mcp.errors import ValidationError
from tests.test_duckquery import PARQUET_URL


class _Counted:
    """Wraps whatever the engine hands back and records how many rows each fetch took."""

    def __init__(self, inner, fetched: list[int]):
        self._inner = inner
        self._fetched = fetched

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == "fetchall":

            def fetchall():
                rows = attr()
                self._fetched.append(len(rows))
                return rows

            return fetchall
        if callable(attr):

            def call(*a, **k):
                out = attr(*a, **k)
                return _Counted(out, self._fetched) if hasattr(out, "fetchall") else out

            return call
        return attr


@pytest.fixture
def fetched(monkeypatch) -> list[int]:
    """The row count of every fetch the engine makes, in order."""
    counts: list[int] = []
    real = duckquery._connect
    monkeypatch.setattr(duckquery, "_connect", lambda u, f: _Counted(real(u, f), counts))
    return counts


async def test_a_trailing_comment_cannot_lift_the_row_cap(fetched) -> None:
    """The cap was appended to the user's text as ``) LIMIT n``, so a query ending in
    ``) --`` commented it out and the whole result was fetched into Python (a cross join
    of the source with itself, unbounded). The cap now sits on the parsed query."""
    # Positive control: the same rows asked for plainly are fetched to the cap plus one.
    ok = await duckquery.run_sql(
        PARQUET_URL, "sample.parquet", "SELECT * FROM range(10) t(i)", row_cap=2
    )
    assert ok["rows"] == [{"i": 0}, {"i": 1}] and ok["truncated"] is True
    assert fetched == [3]
    fetched.clear()
    escape = "SELECT * FROM range(10) t(i)) --"
    try:
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", escape, row_cap=2)
    except duckdb.ParserException:
        pass
    else:
        pytest.fail(f"an unbalanced query was run; fetches: {fetched}")
    assert fetched == [], f"fetched {fetched} rows past a cap of 2"


async def test_a_query_ending_in_a_comment_is_answered(fetched) -> None:
    """The same appended ``) LIMIT n`` made a query ending in a ``--`` comment a syntax
    error: the comment swallowed the closing parenthesis."""
    out = await duckquery.run_sql(
        PARQUET_URL, "sample.parquet", "SELECT * FROM range(10) t(i) -- ten rows", row_cap=2
    )
    assert out["rows"] == [{"i": 0}, {"i": 1}] and out["truncated"] is True
    assert fetched == [3]


async def test_a_semicolon_inside_a_string_is_one_statement() -> None:
    """``;`` was refused wherever it appeared, so a filter on a ``;``-separated value
    (GO terms, keyword lists) was refused as "a single statement only"."""
    out = await duckquery.run_sql(
        PARQUET_URL, "sample.parquet", "SELECT name, 'GO:1;GO:2' AS go FROM data WHERE id = 1;"
    )
    assert out["rows"] == [{"name": "a", "go": "GO:1;GO:2"}]
    with pytest.raises(
        ValidationError, match=r"^\[ValidationError\] operate sql accepts a single statement only$"
    ):
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    "query",
    [
        "FROM data WHERE id = 1",
        "-- the first row\nSELECT name FROM data WHERE id = 1",
        "  with t as (select name from data where id = 1) select * from t ;  ",
    ],
)
async def test_every_query_form_duckdb_parses_as_a_select_is_answered(query) -> None:
    out = await duckquery.run_sql(PARQUET_URL, "sample.parquet", query)
    assert [r["name"] for r in out["rows"]] == ["a"]


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE data",
        "COPY data TO 'x.csv'",
        "SET threads = 1",
        "WITH t AS (SELECT 1) DELETE FROM data",
        "EXPLAIN SELECT 1",
        "",
        " ; ",
    ],
)
async def test_anything_but_a_query_is_refused_before_any_read(query, fetched) -> None:
    with pytest.raises(
        ValidationError,
        match=r"^\[ValidationError\] operate sql accepts a read-only SELECT/WITH query only$",
    ):
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", query)
    assert fetched == []
    ok = await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT id FROM data")
    assert len(ok["rows"]) == 3
