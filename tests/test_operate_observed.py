# tests/test_operate_observed.py
"""What `operate.run` observably does: which file it picks, the exact refusals, what it
hands each engine, the result byte cap and the wall-clock limit (#88 burn-down of
`operate`). The engines run for real on local fixtures unless a test says otherwise."""

import asyncio
import json
import os
import pathlib
import re

import httpx
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("fsspec")

from data_aggregator_mcp import duckquery, egress, operate, router, tool_specs
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import DataResource, FileEntry

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET = FileEntry(name="sample.parquet", url=(FX / "sample.parquet").as_uri())


@pytest.fixture
def record(monkeypatch):
    """Install `files` as the record every resolve returns; return the resolve calls."""
    calls: list[tuple] = []

    def _install(*files: FileEntry) -> list[tuple]:
        res = DataResource(
            id="zenodo:1", source="zenodo", kind="dataset", title="t", files=list(files)
        )

        async def fake_resolve(client, rid):
            calls.append((client, rid))
            return res

        monkeypatch.setattr(router, "resolve", fake_resolve)
        monkeypatch.setattr(operate, "OPERATE_AVAILABLE", True)
        monkeypatch.setenv("DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS", "1")
        return calls

    return _install


def _csv(tmp_path: pathlib.Path, name: str, rows: int) -> FileEntry:
    path = tmp_path / name
    path.write_text("i\n" + "".join(f"{k}\n" for k in range(rows)))
    return FileEntry(name=name, url=path.as_uri())


# --- which file -------------------------------------------------------------------------


async def test_file_param_picks_the_named_file(record, tmp_path):
    record(PARQUET, _csv(tmp_path, "five.csv", 5), FileEntry(name="notes.txt", url="https://h/n"))
    async with httpx.AsyncClient() as c:
        out = await operate.run(
            c, "zenodo:1", "sql", file="five.csv", query="SELECT count(*) AS n FROM data"
        )
        assert out["rows"] == [{"n": 5}] and out["file"] == "five.csv"
        out = await operate.run(
            c, "zenodo:1", "sql", file="sample.parquet", query="SELECT count(*) AS n FROM data"
        )
        assert out["rows"] == [{"n": 3}] and out["file"] == "sample.parquet"
        with pytest.raises(
            OperateNotSupportedError,
            match=r"^\[OperateNotSupportedError\] file 'nope\.csv' not found in record$",
        ):
            await operate.run(c, "zenodo:1", "schema", file="nope.csv")
        need = re.escape(str(operate.TABULAR_EXTS))
        with pytest.raises(
            OperateNotSupportedError,
            match=rf"^\[OperateNotSupportedError\] file 'notes\.txt' is not an operable tabular file \(need {need}\)$",
        ):
            await operate.run(c, "zenodo:1", "schema", file="notes.txt")


async def test_without_a_file_param_the_record_must_hold_exactly_one(record, tmp_path):
    two = _csv(tmp_path, "two.csv", 2)
    async with httpx.AsyncClient() as c:
        record(PARQUET, two, FileEntry(name="notes.txt", url="https://h/n"))
        with pytest.raises(
            OperateNotSupportedError,
            match=r"^\[OperateNotSupportedError\] record has multiple operable files; pass "
            r"file=<name> — options: sample\.parquet, two\.csv$",
        ):
            await operate.run(c, "zenodo:1", "schema")
        record(FileEntry(name="notes.txt", url="https://h/n"), FileEntry(name="x.csv", url=None))
        with pytest.raises(
            OperateNotSupportedError,
            match=r"^\[OperateNotSupportedError\] no operable tabular file in this record; "
            r"resolve it and fetch instead$",
        ):
            await operate.run(c, "zenodo:1", "schema")
        record(FileEntry(name="notes.txt", url="https://h/n"), two)
        out = await operate.run(c, "zenodo:1", "head", n=5)
    assert out["file"] == "two.csv" and out["rows"] == [{"i": 0}, {"i": 1}]


async def test_resolve_gets_the_callers_client_and_id(record):
    calls = record(PARQUET)
    async with httpx.AsyncClient() as c:
        await operate.run(c, "zenodo:1", "schema")
    assert calls == [(c, "zenodo:1")]


# --- refusals before any I/O --------------------------------------------------------------


async def test_refusals_name_the_problem(record, monkeypatch):
    calls = record(PARQUET)
    modes = re.escape(str(operate.OPERATE_MODES))
    async with httpx.AsyncClient() as c:
        with pytest.raises(
            ValidationError,
            match=rf"^\[ValidationError\] unknown op 'describe'; expected one of {modes}$",
        ):
            await operate.run(c, "zenodo:1", "describe")
        with pytest.raises(
            ValidationError, match=r"^\[ValidationError\] op='sql' requires a query$"
        ):
            await operate.run(c, "zenodo:1", "sql")
        for bad in (True, 1.0, "5"):
            with pytest.raises(
                ValidationError, match=r"^\[ValidationError\] n must be a positive integer; got "
            ):
                await operate.run(c, "zenodo:1", "head", n=bad)
        assert calls == []  # every refusal above came before the record was resolved
        out = await operate.run(c, "zenodo:1", "head", n=1)
        assert len(out["rows"]) == 1
        monkeypatch.setattr(operate, "OPERATE_AVAILABLE", False)
        with pytest.raises(
            OperateNotSupportedError,
            match=rf"^\[OperateNotSupportedError\] {re.escape(operate.MISSING_EXTRA_MSG)}$",
        ):
            await operate.run(c, "zenodo:1", "schema")


async def test_url_gates_name_the_file(record, monkeypatch):
    monkeypatch.delenv(egress.ALLOW_PRIVATE_ENV)  # conftest turns the egress guard off
    egress._clear_cache()
    async with httpx.AsyncClient() as c:
        record(FileEntry(name="x.csv", url="http://[bad/x.csv"))
        with pytest.raises(
            ValidationError, match=r"^\[ValidationError\] operate 'x\.csv': malformed URL "
        ):
            await operate.run(c, "zenodo:1", "schema")
        record(FileEntry(name="x.csv", url="ftp://h/x.csv"))
        with pytest.raises(
            OperateNotSupportedError,
            match=r"^\[OperateNotSupportedError\] URL scheme 'ftp' is not allowed for operate "
            r"\(only http/https\): ftp://h/x\.csv$",
        ):
            await operate.run(c, "zenodo:1", "schema")
        # http and https both pass the scheme gate and reach the egress check, which
        # refuses a loopback address in the file's own name. The step after the check is
        # a sentinel, so a URL let past it fails here instead of leaving the process.
        monkeypatch.setattr(operate, "_source_size", _reached)
        for scheme in ("http", "HTTPS"):
            url = f"{scheme}://127.0.0.1:9/x.csv"
            record(FileEntry(name="x.csv", url=url))
            with pytest.raises(
                ValidationError,
                match=rf"^\[ValidationError\] operate 'x\.csv': '127\.0\.0\.1' resolves to the "
                rf"non-public address 127\.0\.0\.1; refusing to fetch {re.escape(url)}\.",
            ):
                await operate.run(c, "zenodo:1", "head")
        # positive controls: a public address gets past the check; a permitted file://
        # fixture operates
        record(FileEntry(name="x.csv", url="https://8.8.8.8/x.csv"))
        with pytest.raises(_ReachedTheDownload, match=r"^https://8\.8\.8\.8/x\.csv$"):
            await operate.run(c, "zenodo:1", "head")
        record(PARQUET)
        assert (await operate.run(c, "zenodo:1", "schema"))["file"] == "sample.parquet"


class _ReachedTheDownload(RuntimeError):
    pass


def _reached(url: str):
    raise _ReachedTheDownload(url)


# --- what each engine is handed ---------------------------------------------------------


async def test_source_ceiling_gates_head_sql_peek_at_its_edge(record, monkeypatch):
    record(PARQUET)
    size = os.path.getsize(FX / "sample.parquet")
    async with httpx.AsyncClient() as c:
        monkeypatch.setattr(operate, "SOURCE_BYTE_CEILING", size - 1)
        for op, kw in (("head", {}), ("sql", {"query": "SELECT 1"}), ("peek", {})):
            with pytest.raises(
                OperateNotSupportedError,
                match=rf"^\[OperateNotSupportedError\] 'sample\.parquet' is {size} bytes, over the "
                rf"operate ceiling of {size - 1}; head/sql load the whole file into memory — use fetch instead$",
            ):
                await operate.run(c, "zenodo:1", op, **kw)
        monkeypatch.setattr(operate, "SOURCE_BYTE_CEILING", size)  # at the ceiling: allowed
        for op, kw in (("head", {}), ("sql", {"query": "SELECT 1 AS one"}), ("peek", {})):
            assert (await operate.run(c, "zenodo:1", op, **kw))["op"] == op


async def test_head_projects_the_requested_columns(record):
    record(PARQUET)
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "head", n=2, columns=["name"])
    assert [col["name"] for col in out["columns"]] == ["name"]
    assert out["rows"] == [{"name": "a"}, {"name": "b"}]


async def test_row_cap_bounds_sql_and_head(record, monkeypatch, tmp_path):
    record(_csv(tmp_path, "five.csv", 5))
    monkeypatch.setattr(operate, "ROW_CAP", 2)
    async with httpx.AsyncClient() as c:
        sql = await operate.run(c, "zenodo:1", "sql", query="SELECT * FROM data")
        head = await operate.run(c, "zenodo:1", "head", n=4)
    assert sql["rows"] == [{"i": 0}, {"i": 1}] and sql["truncated"] is True
    assert head["rows"] == [{"i": 0}, {"i": 1}]


async def test_default_row_count_is_the_advertised_one(record, tmp_path):
    tool = next(t for t in tool_specs.TOOLS if t.name == "operate")
    assert tool.input_schema["properties"]["n"]["default"] == operate.DEFAULT_ROWS == 20
    record(_csv(tmp_path, "many.csv", operate.DEFAULT_ROWS + 5))
    async with httpx.AsyncClient() as c:
        head = await operate.run(c, "zenodo:1", "head")
        preview = await operate.run(c, "zenodo:1", "preview")
    assert len(head["rows"]) == len(preview["rows"]) == operate.DEFAULT_ROWS


# --- the result byte cap ------------------------------------------------------------------


def _row_bytes(row: dict) -> int:
    return len(json.dumps(row).encode())


def test_byte_cap_keeps_every_row_up_to_its_edge(monkeypatch):
    rows = [{"v": "é" * k} for k in (1, 2, 3)]  # multi-byte: the cap counts bytes, not chars
    edge = sum(_row_bytes(r) for r in rows)
    monkeypatch.setattr(operate, "RESULT_BYTE_CAP", edge)
    at_edge = {"rows": list(rows), "truncated": False}
    assert operate._cap_result_bytes(at_edge) == {"rows": rows, "truncated": False}
    monkeypatch.setattr(operate, "RESULT_BYTE_CAP", edge - 1)
    over = {"rows": list(rows), "truncated": False}
    assert operate._cap_result_bytes(over) == {"rows": rows[:2], "truncated": True}


def test_byte_cap_leaves_rowless_results_alone(monkeypatch):
    monkeypatch.setattr(operate, "RESULT_BYTE_CAP", 0)
    assert operate._cap_result_bytes({"columns": ["a"]}) == {"columns": ["a"]}
    assert operate._cap_result_bytes({"rows": []}) == {"rows": []}
    assert operate._cap_result_bytes({"rows": [{"a": 1}]}) == {"rows": [], "truncated": True}


# --- the wall-clock limit -----------------------------------------------------------------


async def test_wall_clock_limit_cancels_the_engine_call(record, monkeypatch):
    record(PARQUET)
    seen: list[str] = []

    async def slow_sql(url, file, query, *, row_cap):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
        return {"columns": [], "rows": [], "truncated": False}

    monkeypatch.setattr(duckquery, "run_sql", slow_sql)
    monkeypatch.setattr(operate, "WALL_TIMEOUT_S", 0.01)
    async with httpx.AsyncClient() as c:
        with pytest.raises(
            OperateNotSupportedError,
            match=r"^\[OperateNotSupportedError\] operate op='sql' exceeded 0\.01s wall-clock limit$",
        ):
            await operate.run(c, "zenodo:1", "sql", query="SELECT 1")
        assert seen == ["cancelled"]
        monkeypatch.setattr(operate, "WALL_TIMEOUT_S", 30.0)  # positive control
        out = await operate.run(c, "zenodo:1", "sql", query="SELECT 1")
    assert out == {
        "columns": [],
        "rows": [],
        "truncated": False,
        "file": "sample.parquet",
        "op": "sql",
    }
