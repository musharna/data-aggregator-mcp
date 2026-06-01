# tests/test_operate.py
import os
import pathlib

import httpx
import pytest

from data_aggregator_mcp import operate, router
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import DataResource, FileEntry

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()


def test_operate_available_is_bool():
    assert isinstance(operate.OPERATE_AVAILABLE, bool)


def test_missing_extra_message_names_the_extra():
    assert "data-aggregator-mcp[operate]" in operate.MISSING_EXTRA_MSG


def _res(files):
    return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t", files=files)


@pytest.fixture
def patch_resolve(monkeypatch):
    def _install(resource):
        async def fake_resolve(client, rid):
            return resource

        monkeypatch.setattr(router, "resolve", fake_resolve)
        monkeypatch.setattr(operate, "OPERATE_AVAILABLE", True)

    return _install


@pytest.mark.asyncio
async def test_sql_op_end_to_end(patch_resolve):
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "sql", query="SELECT name FROM data WHERE temp > 30")
    assert {r["name"] for r in out["rows"]} == {"b", "c"}
    assert out["file"] == "sample.parquet" and out["op"] == "sql"


@pytest.mark.asyncio
async def test_result_byte_cap_trims_wide_results(patch_resolve, monkeypatch):
    # The row cap bounds count; RESULT_BYTE_CAP bounds total bytes. With a tiny
    # cap, a multi-row result is trimmed and flagged truncated.
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    monkeypatch.setattr(operate, "RESULT_BYTE_CAP", 40)
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "sql", query="SELECT * FROM data")
    assert out["truncated"] is True
    assert 0 < len(out["rows"]) < 3  # at least one kept, not all three


@pytest.mark.asyncio
async def test_single_operable_file_auto_selected(patch_resolve):
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "schema")
    assert {col["name"] for col in out["columns"]} == {"id", "name", "temp"}


@pytest.mark.asyncio
async def test_ambiguous_files_require_file_param(patch_resolve):
    patch_resolve(
        _res(
            [
                FileEntry(name="a.parquet", url=PARQUET_URL),
                FileEntry(name="b.parquet", url=PARQUET_URL),
            ]
        )
    )
    async with httpx.AsyncClient() as c:
        with pytest.raises(OperateNotSupportedError):
            await operate.run(c, "zenodo:1", "schema")


@pytest.mark.asyncio
async def test_non_tabular_file_fails_loud(patch_resolve):
    patch_resolve(_res([FileEntry(name="img.png", url="https://h/img.png")]))
    async with httpx.AsyncClient() as c:
        with pytest.raises(OperateNotSupportedError):
            await operate.run(c, "zenodo:1", "schema")


@pytest.mark.asyncio
async def test_sql_without_query_rejected(patch_resolve):
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValidationError):
            await operate.run(c, "zenodo:1", "sql")


@pytest.mark.asyncio
async def test_unknown_op_rejected(patch_resolve):
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValidationError):
            await operate.run(c, "zenodo:1", "describe")


@pytest.mark.asyncio
async def test_source_over_ceiling_rejected(patch_resolve, monkeypatch):
    # head/sql load the whole file into RAM; an oversized source must fail loud, not OOM.
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    monkeypatch.setattr(operate, "SOURCE_BYTE_CEILING", 10)  # fixture is ~1KB > 10
    async with httpx.AsyncClient() as c:
        with pytest.raises(OperateNotSupportedError):
            await operate.run(c, "zenodo:1", "sql", query="SELECT * FROM data")


@pytest.mark.asyncio
async def test_schema_op_not_size_gated(patch_resolve, monkeypatch):
    # schema/preview use the footer/sniff (no full load), so the ceiling must NOT block them.
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    monkeypatch.setattr(operate, "SOURCE_BYTE_CEILING", 10)
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "schema")
    assert "columns" in out


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# A small, stable, openly-downloadable Parquet on the HuggingFace CDN.
_LIVE_PARQUET = (
    "https://huggingface.co/datasets/mteb/tweet_sentiment_extraction/resolve/refs%2F"
    "convert%2Fparquet/default/test/0000.parquet"
)


@_live_only
@pytest.mark.asyncio
async def test_live_operate_sql_no_full_download(monkeypatch):
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
        sch = await operate.run(c, "hf:live", "schema")
        assert sch["columns"]
        rows = await operate.run(c, "hf:live", "sql", query="SELECT * FROM data LIMIT 5")
        assert len(rows["rows"]) == 5
