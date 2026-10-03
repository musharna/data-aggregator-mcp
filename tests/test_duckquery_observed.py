# tests/test_duckquery_observed.py
"""What duckquery observably does: the statements it accepts and refuses, the rows it
fetches, and the exact shape of what it returns."""

import asyncio
import contextlib
import gc
import threading

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import duckdb
import httpx

from data_aggregator_mcp import duckquery, operate
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import FileEntry
from tests.test_duckquery import PARQUET_URL
from tests.test_operate import _LIVE_PARQUET, _live_only, _res, patch_resolve  # noqa: F401


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
    monkeypatch.setattr(duckquery, "_connect", lambda u, f, o: _Counted(real(u, f, o), counts))
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


async def test_peek_profiles_a_file_with_a_header_and_no_rows(tmp_path) -> None:
    """DuckDB's SUMMARIZE answers NULL for the null percentage of an empty column, and
    ``float(None)`` escaped as a bare TypeError, so peek could not profile a header-only
    file. Positive control: the same file with one row is profiled with a real float."""
    empty = tmp_path / "empty.csv"
    empty.write_text("id,name\n")
    out = await duckquery.run_peek(empty.as_uri(), "empty.csv")
    assert out["row_count"] == 0
    assert [
        (c["column_name"], c["null_percentage"], c["approx_unique"]) for c in out["columns"]
    ] == [
        ("id", None, 0),
        ("name", None, 0),
    ]
    one = tmp_path / "one.csv"
    one.write_text("id,name\n1,a\n")
    out = await duckquery.run_peek(one.as_uri(), "one.csv")
    assert [c["null_percentage"] for c in out["columns"]] == [0.0, 0.0]


# ---------------------------------------------------------------------------
# The shape of what comes back
# ---------------------------------------------------------------------------

_SAMPLE_COLUMNS = [
    {"name": "id", "type": "BIGINT"},
    {"name": "name", "type": "VARCHAR"},
    {"name": "temp", "type": "DOUBLE"},
]
_SAMPLE_ROWS = [
    {"id": 1, "name": "a", "temp": 29.5},
    {"id": 2, "name": "b", "temp": 31.0},
    {"id": 3, "name": "c", "temp": 33.2},
]


async def test_sql_rows_and_columns_are_mapped_exactly(fetched) -> None:
    """Each column carries its DuckDB type, each row maps names to values, and a result
    of exactly the cap is not truncated (the cap plus one row was fetched to tell)."""
    out = await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT * FROM data", row_cap=3)
    assert out == {"columns": _SAMPLE_COLUMNS, "rows": _SAMPLE_ROWS, "truncated": False}
    assert fetched == [3]
    out = await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT * FROM data", row_cap=2)
    assert out == {"columns": _SAMPLE_COLUMNS, "rows": _SAMPLE_ROWS[:2], "truncated": True}


async def test_head_projects_the_named_columns_in_order() -> None:
    out = await duckquery.run_head(PARQUET_URL, "sample.parquet", n=2, columns=["temp", "name"])
    assert out == {
        "columns": [{"name": "temp", "type": "DOUBLE"}, {"name": "name", "type": "VARCHAR"}],
        "rows": [{"temp": 29.5, "name": "a"}, {"temp": 31.0, "name": "b"}],
        "truncated": True,
    }


async def test_head_quotes_a_column_name_that_holds_a_double_quote(tmp_path) -> None:
    """A real column named ``we"ird`` is read through its doubled quote; the same quoting
    keeps a crafted name one (missing) identifier instead of new SQL."""
    p = tmp_path / "q.csv"
    p.write_text('id,"we""ird"\n1,x\n')
    out = await duckquery.run_head(p.as_uri(), "q.csv", n=5, columns=['we"ird'])
    assert out["rows"] == [{'we"ird': "x"}]
    with pytest.raises(duckdb.BinderException, match='we"ird" FROM data; --'):
        await duckquery.run_head(p.as_uri(), "q.csv", n=5, columns=['we"ird" FROM data; --'])


async def test_a_source_url_with_a_quote_is_read_as_one_string() -> None:
    """The source URL comes from the record: the server is asked for exactly that path
    and nothing else is read. DuckDB reads the downloaded copy, whose path has no quote,
    so this does not reach the SQL literal (the next test does)."""
    from tests.test_duckquery import _listener

    with _listener() as (port, hits):
        url = f"http://127.0.0.1:{port}/o'brien/source.csv"
        out = await duckquery.run_sql(url, "source.csv", "SELECT * FROM data")
    assert out["rows"] == [{"col": "legit"}]
    assert set(hits) == {"/o'brien/source.csv"}


async def test_a_local_source_path_with_a_quote_stays_inside_the_sql_string(tmp_path) -> None:
    """A local source is handed to DuckDB as given, inside a ``'...'`` literal that is
    read before the lockdown. A ``'`` in the path must be doubled, not end the string:
    each quoted path reads its own file and nothing else."""
    rows = {}
    for name in ("o'brien", "it''s"):
        p = tmp_path / name / "q.csv"
        p.parent.mkdir()
        p.write_text(f"id,dir\n1,{len(name)}\n")
        out = await duckquery.run_sql(str(p), "q.csv", "SELECT * FROM data")
        rows[name] = out["rows"]
    assert rows == {"o'brien": [{"id": 1, "dir": 7}], "it''s": [{"id": 1, "dir": 5}]}


async def test_peek_profile_is_mapped_field_by_field() -> None:
    out = await duckquery.run_peek(PARQUET_URL, "sample.parquet")
    assert out["row_count"] == 3
    by_name = {c["column_name"]: c for c in out["columns"]}
    assert by_name["id"] == {
        "column_name": "id",
        "column_type": "BIGINT",
        "null_percentage": 0.0,
        "approx_unique": 3,
        "min": "1",
        "max": "3",
        "avg": "2.0",
        "std": "1.0",
        "q25": "1",
        "q50": "2",
        "q75": "3",
    }
    assert by_name["name"] == {
        "column_name": "name",
        "column_type": "VARCHAR",
        "null_percentage": 0.0,
        "approx_unique": 3,
        "min": "a",
        "max": "c",
        "avg": None,
        "std": None,
        "q25": None,
        "q50": None,
        "q75": None,
    }
    temp = by_name["temp"]
    assert (temp["column_type"], temp["min"], temp["max"]) == ("DOUBLE", "29.5", "33.2")
    assert abs(float(temp["avg"]) - 31.2333) < 1e-3


@_live_only
async def test_live_sql_over_a_real_remote_parquet_keeps_its_cap(monkeypatch) -> None:
    """Real execution: operate downloads a real Parquet over HTTPS, answers a FROM-first
    query that ends in a comment, caps a cross join of the file with itself at the row
    cap, and refuses the ``) --`` form that used to lift the cap, before any download."""
    import httpx

    from data_aggregator_mcp import operate, router
    from data_aggregator_mcp.models import DataResource, FileEntry

    res = DataResource(
        id="hf:live",
        source="huggingface",
        kind="dataset",
        title="t",
        files=[FileEntry(name="0000.parquet", url=_LIVE_PARQUET)],
    )

    async def fake_resolve(client, rid):
        return res

    monkeypatch.setattr(router, "resolve", fake_resolve)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        out = await operate.run(c, "hf:live", "sql", query="FROM data a, data b -- every pair")
        assert len(out["rows"]) == operate.ROW_CAP and out["truncated"] is True
        with pytest.raises(duckdb.ParserException):
            await operate.run(c, "hf:live", "sql", query="SELECT * FROM data a, data b) --")


# How long to wait for an engine thread to end. A hang guard, not a time budget: the old
# code's thread ran to the end of its query after a timeout; the fixed one ends at an
# interrupt.
_THREAD_END_GUARD_S = 60


class _Worker:
    """How each engine thread ended, recorded from inside the thread."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.ended = threading.Semaphore(0)
        self.cons: list = []

    async def wait_for_end(self) -> None:
        await asyncio.to_thread(self.ended.acquire, timeout=_THREAD_END_GUARD_S)


@pytest.fixture
def worker(monkeypatch):
    w = _Worker()
    real_run, real_connect = duckquery._run, duckquery._connect

    def run(*args):
        try:
            out = real_run(*args)
        except Exception as exc:
            w.outcomes.append(type(exc).__name__)
            w.ended.release()
            raise
        w.outcomes.append("finished")
        w.ended.release()
        return out

    def connect(*args):
        con = real_connect(*args)
        w.cons.append(con)
        return con

    monkeypatch.setattr(duckquery, "_run", run)
    monkeypatch.setattr(duckquery, "_connect", connect)
    yield w
    # Stop anything the code under test left running, so a failing run cannot hang the
    # session; the assertions were made before this point.
    for con in w.cons:
        with contextlib.suppress(duckdb.ConnectionException):
            con.interrupt()


# About 2 s of work on a laptop: 40 times the interrupt interval, so a working interrupt
# always lands first, and short enough that a mutant that never interrupts fails fast.
_LONG = (
    "SELECT count(*) FROM range(700) a, range(700) b, range(700) c"
    " WHERE a.range + b.range + c.range = -1"
)


def test_an_interrupt_skips_a_connection_already_closed() -> None:
    """A thread may close its connection between two interrupts; that must not stop the
    interrupts reaching the connections still running."""
    closed = duckdb.connect()
    closed.close()
    running = duckdb.connect()
    ended: list[str] = []

    def endless() -> None:
        try:
            running.execute(_LONG)
        except duckdb.InterruptException:
            ended.append("interrupted")

    t = threading.Thread(target=endless)
    t.start()
    try:
        for _ in range(int(_THREAD_END_GUARD_S / duckquery._INTERRUPT_EVERY_S)):
            if not t.is_alive():
                break
            duckquery._interrupt([closed, running])
            t.join(duckquery._INTERRUPT_EVERY_S)
        assert ended == ["interrupted"]
    finally:
        running.interrupt()
        t.join()


async def test_a_limit_reached_before_the_connection_opens_still_stops_it() -> None:
    """The limit can fall before the worker thread has opened its connection (the thread
    pool busy, the thread not yet started). One interrupt then reaches nothing, so the
    interrupts go on until the thread ends and stop the connection it opens later."""
    may_open = threading.Event()
    outcomes: list[str] = []
    ended = threading.Semaphore(0)
    cons: list = []

    def work(sql: str, opened) -> str:
        may_open.wait(_THREAD_END_GUARD_S)
        con = duckdb.connect()
        cons.append(con)
        opened(con)
        try:
            out = str(con.execute(sql).fetchone()[0])
            outcomes.append("finished")
            return out
        except duckdb.InterruptException:
            outcomes.append("InterruptException")
            raise
        finally:
            con.close()
            ended.release()

    # Positive control: a call that is not cancelled runs to its answer.
    may_open.set()
    assert await duckquery._interruptible(work, "SELECT 41 + 1") == "42"
    await asyncio.to_thread(ended.acquire, timeout=_THREAD_END_GUARD_S)
    may_open.clear()
    try:
        call = asyncio.ensure_future(duckquery._interruptible(work, _LONG))
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        may_open.set()
        await asyncio.to_thread(ended.acquire, timeout=_THREAD_END_GUARD_S)
        assert outcomes == ["finished", "InterruptException"]
        await asyncio.gather(*duckquery._STOPPING)
    finally:
        # A failing run must not leave its thread waiting or querying.
        may_open.set()
        for con in cons:
            with contextlib.suppress(duckdb.ConnectionException):
                con.interrupt()


# 3000**3 joined rows: many times the 2 s limit (the old code ran it to the end, ~15 s).
_ENDLESS = "SELECT count(*) FROM data a, data b, data c WHERE a.i + b.i + c.i = -1"


async def test_a_query_past_the_wall_clock_limit_is_interrupted(
    request, worker, monkeypatch, tmp_path
) -> None:
    """operate's wall-clock limit cancelled the await, not the thread doing the work: the
    timeout was reported and the query ran on in the server (to 13.65 s after a timeout
    reported at 1.14 s). The thread is now interrupted, and its outcome is taken, so
    asyncio has no unretrieved error to report."""
    src = tmp_path / "n.csv"
    src.write_text("i\n" + "".join(f"{k}\n" for k in range(3000)))
    install = request.getfixturevalue("patch_resolve")  # a parameter would shadow the import
    install(_res([FileEntry(name="n.csv", url=src.as_uri())]))
    monkeypatch.setattr(operate, "WALL_TIMEOUT_S", 2.0)
    loop = asyncio.get_running_loop()
    unreported: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: unreported.append(ctx))
    try:
        async with httpx.AsyncClient() as c:
            # Positive control: a query inside the limit is answered and its thread ends.
            ok = await operate.run(c, "zenodo:1", "sql", query="SELECT count(*) AS n FROM data")
            assert ok["rows"] == [{"n": 3000}]
            await worker.wait_for_end()
            assert worker.outcomes == ["finished"]
            with pytest.raises(
                OperateNotSupportedError,
                match=r"\] operate op='sql' exceeded 2\.0s wall-clock limit$",
            ):
                await operate.run(c, "zenodo:1", "sql", query=_ENDLESS)
        await worker.wait_for_end()
        assert worker.outcomes == ["finished", "InterruptException"]
        await asyncio.gather(*duckquery._STOPPING)
        gc.collect()
        assert unreported == []
    finally:
        loop.set_exception_handler(None)


@_live_only
async def test_live_a_query_past_the_wall_clock_limit_is_interrupted(worker, monkeypatch) -> None:
    """Real execution: over a real Parquet downloaded over HTTPS, a query past the limit
    ends its thread at an interrupt, and one inside the limit is answered."""
    from data_aggregator_mcp import router
    from data_aggregator_mcp.models import DataResource

    res = DataResource(
        id="hf:live",
        source="huggingface",
        kind="dataset",
        title="t",
        files=[FileEntry(name="0000.parquet", url=_LIVE_PARQUET)],
    )

    async def fake_resolve(client, rid):
        return res

    monkeypatch.setattr(router, "resolve", fake_resolve)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        ok = await operate.run(c, "hf:live", "sql", query="SELECT count(*) AS n FROM data")
        assert ok["rows"] == [{"n": 3432}]
        await worker.wait_for_end()
        monkeypatch.setattr(operate, "WALL_TIMEOUT_S", 3.0)
        endless = (
            "FROM data a, data b, data c SELECT count(*) WHERE a.label + b.label + c.label = -1"
        )
        with pytest.raises(OperateNotSupportedError, match=r"exceeded 3\.0s wall-clock limit$"):
            await operate.run(c, "hf:live", "sql", query=endless)
    await worker.wait_for_end()
    assert worker.outcomes == ["finished", "InterruptException"]
