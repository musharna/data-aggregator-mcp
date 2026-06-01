# Operate-on-Data (Tabular Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 5th MCP tool `operate` that inspects/queries a remote tabular file (Parquet/CSV/TSV) without downloading it (`schema`/`preview`/`head`/`sql`), plus a `DataResource.access_modes` capability field.

**Architecture:** `operate(op, id, file=…)` resolves the id via the existing `router.resolve`, selects a file from its manifest, verifies operability, then dispatches to one of two engines — pyarrow footer / CSV sniff for `schema`+`preview`, hardened DuckDB+httpfs for `head`+`sql`. Heavy sync libs run in `asyncio.to_thread`. `duckdb`/`pyarrow`/`fsspec` ship as an optional `[operate]` extra so the base install stays light; `access_modes` degrades to `["fetch"]` when the extra is absent.

**Tech Stack:** Python 3.11+, `httpx`, `pydantic` v2, `mcp` SDK (stdio), `duckdb` + `pyarrow` + `fsspec` (optional extra), `pytest`/`pytest-asyncio`.

Spec: `docs/superpowers/specs/2026-06-01-operate-on-data-design.md`.

---

## File Structure

- **Create `src/data_aggregator_mcp/operate.py`** — availability guard + op dispatch (`run()`): resolve → select file → verify → enforce limits → engine → shape result.
- **Create `src/data_aggregator_mcp/duckquery.py`** — hardened DuckDB+httpfs engine for `head`/`sql`.
- **Create `src/data_aggregator_mcp/tabular.py`** — pyarrow Parquet-footer schema + CSV sniff for `schema`/`preview`.
- **Modify `src/data_aggregator_mcp/models.py`** — add `access_modes` field + `derive_access_modes()` helper.
- **Modify `src/data_aggregator_mcp/router.py`** — set `access_modes` in the `resolve` enrich tail.
- **Modify `src/data_aggregator_mcp/errors.py`** — add `OperateNotSupportedError`.
- **Modify `src/data_aggregator_mcp/server.py`** — register the `operate` Tool + add the `operate` dispatch case; note operability in `list_sources`.
- **Modify `pyproject.toml`** — `[operate]` optional extra + version bump.
- **Tests:** `tests/test_tabular.py`, `tests/test_duckquery.py`, `tests/test_operate.py`, plus edits to `tests/test_models.py` (or new), `tests/test_router.py`, `tests/test_server.py`, `tests/test_output_schema_gate.py`, `tests/test_packaging.py`.
- **Fixtures:** `tests/fixtures/sample.parquet`, `tests/fixtures/sample.csv` (tiny, committed).

Resource-limit constants (one home, imported where needed) live in `operate.py`:
`ROW_CAP = 1000`, `RESULT_BYTE_CAP = 5_000_000`, `WALL_TIMEOUT_S = 30.0`, `CSV_SOURCE_CEILING = 100_000_000`.

---

## Task 1: `[operate]` optional extra + availability guard

**Files:**

- Create: `src/data_aggregator_mcp/operate.py`
- Modify: `pyproject.toml:34` (`[project.optional-dependencies]`)
- Test: `tests/test_operate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_operate.py
from data_aggregator_mcp import operate


def test_operate_available_is_bool():
    assert isinstance(operate.OPERATE_AVAILABLE, bool)


def test_missing_extra_message_names_the_extra():
    assert "data-aggregator-mcp[operate]" in operate.MISSING_EXTRA_MSG
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_operate.py -v`
Expected: FAIL (`ModuleNotFoundError: data_aggregator_mcp.operate`).

- [ ] **Step 3: Create the module with a guarded import**

```python
# src/data_aggregator_mcp/operate.py
"""The `operate` tool: inspect/query a remote tabular file without downloading it."""

from __future__ import annotations

ROW_CAP = 1000
RESULT_BYTE_CAP = 5_000_000
WALL_TIMEOUT_S = 30.0
CSV_SOURCE_CEILING = 100_000_000  # CSV has no pushdown; refuse larger sources

MISSING_EXTRA_MSG = (
    "operate-on-data needs the optional extra: install `data-aggregator-mcp[operate]` "
    "(adds duckdb + pyarrow + fsspec)."
)

try:  # the heavy deps are import-guarded so the base install stays light
    import duckdb  # noqa: F401
    import pyarrow  # noqa: F401

    OPERATE_AVAILABLE = True
except ImportError:
    OPERATE_AVAILABLE = False

TABULAR_EXTS = (".parquet", ".pq", ".csv", ".tsv")
OPERATE_MODES = ("schema", "preview", "head", "sql")
```

- [ ] **Step 4: Add the extra to pyproject.toml**

Add after the `dev = [...]` block under `[project.optional-dependencies]`:

```toml
operate = [
    "duckdb>=1.1",
    "pyarrow>=17",
    "fsspec>=2024.6",
]
```

- [ ] **Step 5: Run tests + add the deps to the dev env**

Run: `uv sync --extra dev --extra operate && uv run pytest tests/test_operate.py -v`
Expected: PASS, `OPERATE_AVAILABLE` is `True` in the dev env.

- [ ] **Step 6: Commit**

```bash
git add src/data_aggregator_mcp/operate.py pyproject.toml uv.lock tests/test_operate.py
git commit -m "feat(operate): module scaffold + [operate] optional extra"
```

---

## Task 2: `access_modes` field + `derive_access_modes()` helper

**Files:**

- Modify: `src/data_aggregator_mcp/models.py:69-96` (DataResource), append helper after `derive_version_status`
- Test: `tests/test_models.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py  (append)
from data_aggregator_mcp.models import DataResource, FileEntry, derive_access_modes


def test_access_modes_defaults_empty():
    r = DataResource(id="x:1", source="x", kind="dataset", title="t")
    assert r.access_modes == []


def test_derive_access_modes_tabular_with_extra():
    files = [FileEntry(name="data.parquet", url="https://h/data.parquet")]
    assert derive_access_modes(files, operate=True) == ["fetch", "schema", "preview", "head", "sql"]


def test_derive_access_modes_tabular_without_extra_is_fetch_only():
    files = [FileEntry(name="data.parquet", url="https://h/data.parquet")]
    assert derive_access_modes(files, operate=False) == ["fetch"]


def test_derive_access_modes_non_tabular_is_fetch_only():
    files = [FileEntry(name="img.png", url="https://h/img.png")]
    assert derive_access_modes(files, operate=True) == ["fetch"]


def test_derive_access_modes_no_url_is_empty():
    files = [FileEntry(name="data.parquet", url=None)]
    assert derive_access_modes(files, operate=True) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_models.py -k access_modes -v`
Expected: FAIL (`ImportError: cannot import name 'derive_access_modes'`).

- [ ] **Step 3: Add the field + helper**

In `DataResource` (after the `ro_crate` field at `models.py:96`):

```python
    access_modes: list[str] = Field(default_factory=list)  # best-effort: fetch + operate modes
```

Append to the end of `models.py`:

```python
_TABULAR_EXTS = (".parquet", ".pq", ".csv", ".tsv")


def derive_access_modes(files: list["FileEntry"], *, operate: bool) -> list[str]:
    """Best-effort Tier-1 capability claim for a resolved record.

    ``fetch`` when any file has a download url; the operate modes
    (schema/preview/head/sql) when a tabular file is present AND the [operate]
    extra is installed. Format-dependent modes are *claims* — operate verifies
    them per-file and fails loud if the claim does not hold.
    """
    has_url = any(f.url for f in files)
    if not has_url:
        return []
    modes = ["fetch"]
    if operate and any(
        (f.name or "").lower().endswith(_TABULAR_EXTS) for f in files if f.url
    ):
        modes += ["schema", "preview", "head", "sql"]
    return modes
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_models.py -k access_modes -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/models.py tests/test_models.py
git commit -m "feat(models): access_modes field + derive_access_modes helper"
```

---

## Task 3: populate `access_modes` in `router.resolve`

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (resolve enrich tail, after `if resource.organism:` block near line 425)
- Test: `tests/test_router.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_router.py  (append)
import httpx
import pytest

from data_aggregator_mcp import operate, router
from data_aggregator_mcp.models import DataResource, FileEntry


@pytest.mark.asyncio
async def test_resolve_sets_access_modes(monkeypatch):
    res = DataResource(
        id="zenodo:1", source="zenodo", kind="dataset", title="t",
        files=[FileEntry(name="d.parquet", url="https://h/d.parquet")],
    )

    async def fake_zenodo_resolve(client, rid):
        return res

    monkeypatch.setattr(router.zenodo, "resolve", fake_zenodo_resolve)
    monkeypatch.setattr(operate, "OPERATE_AVAILABLE", True)
    async with httpx.AsyncClient() as c:
        out = await router.resolve(c, "zenodo:1")
    assert "sql" in out.access_modes and "fetch" in out.access_modes
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_router.py -k access_modes -v`
Expected: FAIL (`access_modes == []`).

- [ ] **Step 3: Wire it into resolve's enrich tail**

At the top of `router.py`, add to the imports: `from data_aggregator_mcp import operate`.
In `resolve`, just before the final `return resource` (after taxa/links enrichment, and **before** the `_RESOLVE_CACHE.put`), add:

```python
    resource = resource.model_copy(
        update={
            "access_modes": derive_access_modes(
                resource.files, operate=operate.OPERATE_AVAILABLE
            )
        }
    )
```

Add `derive_access_modes` to the existing `from data_aggregator_mcp.models import ...` line in `router.py`.

(Read the live tail of `resolve` first — keep the existing cache-put and return; only insert the `model_copy` before whichever line stores/returns the final resource.)

- [ ] **Step 4: Run to verify it passes + full router suite**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS (new test + existing all green).

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py tests/test_router.py
git commit -m "feat(router): populate access_modes on resolve"
```

---

## Task 4: `tabular.py` — footer schema + CSV sniff (`schema`/`preview`)

**Files:**

- Create: `src/data_aggregator_mcp/tabular.py`
- Create fixtures: `tests/fixtures/sample.parquet`, `tests/fixtures/sample.csv`
- Test: `tests/test_tabular.py`

- [ ] **Step 1: Build the committed fixtures**

Run once to create them (committed, not generated at test time):

```bash
uv run python - <<'PY'
import pyarrow as pa, pyarrow.parquet as pq, pathlib
pathlib.Path("tests/fixtures").mkdir(parents=True, exist_ok=True)
t = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"], "temp": [29.5, 31.0, 33.2]})
pq.write_table(t, "tests/fixtures/sample.parquet")
pathlib.Path("tests/fixtures/sample.csv").write_text("id,name,temp\n1,a,29.5\n2,b,31.0\n3,c,33.2\n")
PY
```

- [ ] **Step 2: Write the failing test**

```python
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
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_tabular.py -v`
Expected: FAIL (no module `tabular`).

- [ ] **Step 4: Implement `tabular.py`**

```python
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
        cols = [{"name": n, "type": _arrow_type(t)} for n, t in zip(pf.schema_arrow.names, pf.schema_arrow.types)]
        nrows = pf.metadata.num_rows if pf.metadata is not None else None
    return {"format": "parquet", "columns": cols, "row_estimate": nrows}


def _read_head_bytes(url: str, n: int) -> bytes:
    with fsspec.open(url, "rb") as f:
        return f.read(n)


def _schema_csv(url: str) -> dict:
    head = _read_head_bytes(url, _CSV_SNIFF_BYTES).decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(head))
    header = next(reader, [])
    return {"format": "csv", "columns": [{"name": h, "type": "string"} for h in header], "row_estimate": None}


async def schema(url: str, file: str) -> dict:
    fn = _schema_parquet if _is_parquet(file) else _schema_csv
    return await asyncio.to_thread(fn, url)


def _preview_parquet(url: str, n: int) -> dict:
    with fsspec.open(url, "rb") as f:
        pf = pq.ParquetFile(f)
        batch = next(pf.iter_batches(batch_size=n))
        cols = [{"name": n2, "type": _arrow_type(t)} for n2, t in zip(pf.schema_arrow.names, pf.schema_arrow.types)]
    rows = batch.to_pylist()
    return {"format": "parquet", "columns": cols, "rows": rows[:n]}


def _preview_csv(url: str, n: int) -> dict:
    head = _read_head_bytes(url, _CSV_SNIFF_BYTES).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(head))
    rows = []
    for i, row in enumerate(reader):
        if i >= n:
            break
        rows.append(dict(row))
    cols = [{"name": h, "type": "string"} for h in (reader.fieldnames or [])]
    return {"format": "csv", "columns": cols, "rows": rows}


async def preview(url: str, file: str, *, n: int = 20) -> dict:
    fn = _preview_parquet if _is_parquet(file) else _preview_csv
    return await asyncio.to_thread(fn, url, n)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_tabular.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/data_aggregator_mcp/tabular.py tests/test_tabular.py tests/fixtures/sample.parquet tests/fixtures/sample.csv
git commit -m "feat(tabular): footer schema + CSV sniff for schema/preview"
```

---

## Task 5: `duckquery.py` — hardened DuckDB engine (`head`/`sql`)

**Files:**

- Create: `src/data_aggregator_mcp/duckquery.py`
- Test: `tests/test_duckquery.py`

- [ ] **Step 1: Write the failing test (incl. the security cases)**

```python
# tests/test_duckquery.py
import pathlib

import pytest

from data_aggregator_mcp import duckquery
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError

FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()


@pytest.mark.asyncio
async def test_sql_filters_rows():
    out = await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT name FROM data WHERE temp > 30")
    assert {r["name"] for r in out["rows"]} == {"b", "c"}
    assert out["columns"][0]["name"] == "name"


@pytest.mark.asyncio
async def test_head_limits_rows():
    out = await duckquery.run_head(PARQUET_URL, "sample.parquet", n=2, columns=None)
    assert len(out["rows"]) == 2


@pytest.mark.asyncio
async def test_row_cap_marks_truncated():
    out = await duckquery.run_sql(
        PARQUET_URL, "sample.parquet", "SELECT * FROM data", row_cap=2
    )
    assert len(out["rows"]) == 2 and out["truncated"] is True


@pytest.mark.asyncio
async def test_non_select_rejected():
    with pytest.raises(ValidationError):
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", "DROP TABLE data")


@pytest.mark.asyncio
async def test_local_file_read_rejected():
    # A query reaching outside the registered view into the local FS must fail loud,
    # NOT return /etc/passwd contents.
    with pytest.raises((OperateNotSupportedError, ValidationError, Exception)) as ei:
        await duckquery.run_sql(PARQUET_URL, "sample.parquet", "SELECT * FROM read_csv_auto('/etc/passwd')")
    assert "passwd" not in str(ei.value).lower() or "disabled" in str(ei.value).lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_duckquery.py -v`
Expected: FAIL (no module).

- [ ] **Step 3: Implement `duckquery.py`**

```python
# src/data_aggregator_mcp/duckquery.py
"""Hardened DuckDB+httpfs engine for operate's head/sql ops.

The remote file is registered as a read-only view named `data`. The local
filesystem is disabled (httpfs stays enabled for the range reads) and the
configuration is locked, so a user SELECT cannot read local files or write
anything. Only a single SELECT/WITH statement is accepted. Sync calls run in
asyncio.to_thread; the whole call is wall-clock-bounded by the caller.
"""

from __future__ import annotations

import asyncio
import re

from data_aggregator_mcp.errors import ValidationError

_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_PARQUET_EXTS = (".parquet", ".pq")
DEFAULT_ROW_CAP = 1000


def _reader(url: str, file: str) -> str:
    fn = "read_parquet" if file.lower().endswith(_PARQUET_EXTS) else "read_csv_auto"
    safe = url.replace("'", "''")
    return f"{fn}('{safe}')"


def _validate_select(query: str) -> str:
    q = query.strip().rstrip(";")
    if ";" in q:
        raise ValidationError("operate sql accepts a single statement only")
    if not _SELECT_RE.match(q):
        raise ValidationError("operate sql accepts a read-only SELECT/WITH query only")
    return q


def _connect(url: str, file: str):
    import duckdb

    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET disabled_filesystems='LocalFileSystem';")
    con.execute(f"CREATE VIEW data AS SELECT * FROM {_reader(url, file)};")
    con.execute("SET lock_configuration=true;")
    return con


def _run(url: str, file: str, sql: str, row_cap: int) -> dict:
    con = _connect(url, file)
    try:
        rel = con.execute(f"SELECT * FROM ({sql}) LIMIT {row_cap + 1}")
        cols = [{"name": d[0], "type": str(d[1])} for d in rel.description]
        rows = rel.fetchall()
    finally:
        con.close()
    truncated = len(rows) > row_cap
    rows = rows[:row_cap]
    names = [c["name"] for c in cols]
    return {
        "columns": cols,
        "rows": [dict(zip(names, r)) for r in rows],
        "truncated": truncated,
    }


async def run_sql(url: str, file: str, query: str, *, row_cap: int = DEFAULT_ROW_CAP) -> dict:
    sql = _validate_select(query)
    return await asyncio.to_thread(_run, url, file, sql, row_cap)


async def run_head(url: str, file: str, *, n: int, columns: list[str] | None) -> dict:
    proj = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    return await asyncio.to_thread(_run, url, file, f"SELECT {proj} FROM data", n)
```

Add to `errors.py` (after `ValidationError`):

```python
class OperateNotSupportedError(DataAggregatorError):
    """The requested op/file is not operable (not tabular, not range-readable, or
    the [operate] extra is absent)."""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_duckquery.py -v`
Expected: PASS (5 tests). The local-file test passes because `disabled_filesystems` makes DuckDB raise instead of reading `/etc/passwd`.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/duckquery.py src/data_aggregator_mcp/errors.py tests/test_duckquery.py
git commit -m "feat(duckquery): hardened DuckDB+httpfs engine for head/sql"
```

---

## Task 6: `operate.run()` — dispatch, file selection, limits

**Files:**

- Modify: `src/data_aggregator_mcp/operate.py`
- Test: `tests/test_operate.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_operate.py  (append)
import httpx
import pytest

from data_aggregator_mcp import operate, router
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import DataResource, FileEntry

import pathlib
FX = pathlib.Path(__file__).parent / "fixtures"
PARQUET_URL = (FX / "sample.parquet").as_uri()


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


@pytest.mark.asyncio
async def test_single_operable_file_auto_selected(patch_resolve):
    patch_resolve(_res([FileEntry(name="sample.parquet", url=PARQUET_URL)]))
    async with httpx.AsyncClient() as c:
        out = await operate.run(c, "zenodo:1", "schema")
    assert {col["name"] for col in out["columns"]} == {"id", "name", "temp"}


@pytest.mark.asyncio
async def test_ambiguous_files_require_file_param(patch_resolve):
    patch_resolve(_res([
        FileEntry(name="a.parquet", url=PARQUET_URL),
        FileEntry(name="b.parquet", url=PARQUET_URL),
    ]))
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_operate.py -k "op or file or query" -v`
Expected: FAIL (`operate.run` undefined).

- [ ] **Step 3: Implement `operate.run()`**

Append to `src/data_aggregator_mcp/operate.py`:

```python
import asyncio

import httpx

from data_aggregator_mcp import router
from data_aggregator_mcp.errors import OperateNotSupportedError, ValidationError
from data_aggregator_mcp.models import FileEntry


def _operable(f: FileEntry) -> bool:
    return bool(f.url) and (f.name or "").lower().endswith(TABULAR_EXTS)


def _select_file(files: list[FileEntry], requested: str | None) -> FileEntry:
    operable = [f for f in files if _operable(f)]
    if requested is not None:
        for f in files:
            if f.name == requested:
                if not _operable(f):
                    raise OperateNotSupportedError(
                        f"file {requested!r} is not an operable tabular file (need {TABULAR_EXTS})"
                    )
                return f
        raise OperateNotSupportedError(f"file {requested!r} not found in record")
    if len(operable) == 1:
        return operable[0]
    if not operable:
        raise OperateNotSupportedError(
            "no operable tabular file in this record; resolve it and fetch instead"
        )
    raise OperateNotSupportedError(
        "record has multiple operable files; pass file=<name> — options: "
        + ", ".join(f.name for f in operable)
    )


async def run(
    client: httpx.AsyncClient,
    resource_id: str,
    op: str,
    *,
    file: str | None = None,
    query: str | None = None,
    n: int = 20,
    columns: list[str] | None = None,
) -> dict:
    if not OPERATE_AVAILABLE:
        raise OperateNotSupportedError(MISSING_EXTRA_MSG)
    if op not in OPERATE_MODES:
        raise ValidationError(f"unknown op {op!r}; expected one of {OPERATE_MODES}")
    if op == "sql" and not query:
        raise ValidationError("op='sql' requires a query")

    from data_aggregator_mcp import duckquery, tabular

    resource = await router.resolve(client, resource_id)
    target = _select_file(resource.files, file)
    n = min(n, ROW_CAP)

    async def _go() -> dict:
        if op == "schema":
            return await tabular.schema(target.url, target.name)
        if op == "preview":
            return await tabular.preview(target.url, target.name, n=n)
        if op == "head":
            return await duckquery.run_head(target.url, target.name, n=n, columns=columns)
        return await duckquery.run_sql(target.url, target.name, query, row_cap=ROW_CAP)

    try:
        result = await asyncio.wait_for(_go(), timeout=WALL_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise OperateNotSupportedError(
            f"operate op={op!r} exceeded {WALL_TIMEOUT_S}s wall-clock limit"
        ) from exc
    result["file"] = target.name
    result["op"] = op
    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_operate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/operate.py tests/test_operate.py
git commit -m "feat(operate): run() dispatch, file selection, limits, timeout"
```

---

## Task 7: register the `operate` tool in `server.py`

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (add a Tool to `TOOLS` after `list_sources`; add an `operate` case in `_dispatch`)
- Test: `tests/test_server.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py  (append; match the existing import style)
import pytest

from data_aggregator_mcp import server


def test_operate_tool_registered():
    names = {t.name for t in server.TOOLS}
    assert "operate" in names
    op = next(t for t in server.TOOLS if t.name == "operate")
    assert op.inputSchema["required"] == ["op", "id"]
    assert set(op.inputSchema["properties"]["op"]["enum"]) == {"schema", "preview", "head", "sql"}


@pytest.mark.asyncio
async def test_operate_dispatch_routes(monkeypatch):
    async def fake_run(client, rid, op, **kw):
        return {"op": op, "file": "x.parquet", "columns": [], "rows": []}

    monkeypatch.setattr(server.operate, "run", fake_run)
    out = await server._dispatch("operate", {"id": "zenodo:1", "op": "schema"})
    assert out["op"] == "schema"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_server.py -k operate -v`
Expected: FAIL (`operate` not in TOOLS / unknown tool).

- [ ] **Step 3: Add the import, the Tool, and the dispatch case**

In `server.py` imports add: `from data_aggregator_mcp import operate`.

Append to the `TOOLS` list (after the `list_sources` Tool, before the closing `]` at `server.py:406`):

```python
    types.Tool(
        name="operate",
        description=(
            "Inspect or query a remote tabular file (Parquet/CSV/TSV) WITHOUT downloading "
            "it. op='schema' returns columns+types; 'preview' a small sample; 'head' the "
            "first n rows; 'sql' a read-only SELECT against the file (exposed as the view "
            "'data', e.g. \"SELECT * FROM data WHERE x > 1\"). Addresses a file by catalog "
            "id + file name (resolve the id first to see files[] and access_modes). Requires "
            "the [operate] extra; fails loud if the file is not an operable tabular file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["schema", "preview", "head", "sql"]},
                "id": {"type": "string", "description": "DataResource id (e.g. 'zenodo:7654321')"},
                "file": {
                    "type": "string",
                    "description": "File name within the record; optional when exactly one "
                    "operable file is present.",
                },
                "query": {"type": "string", "description": "Read-only SELECT for op='sql'."},
                "n": {"type": "integer", "description": "Row count for head/preview", "default": 20},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional column projection for head.",
                },
            },
            "required": ["op", "id"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
```

In `_dispatch`, add a case inside the `match name:` block (alongside `resolve`/`fetch`):

```python
            case "operate":
                return await operate.run(
                    client,
                    args["id"],
                    args["op"],
                    file=args.get("file"),
                    query=args.get("query"),
                    n=args.get("n", 20),
                    columns=args.get("columns"),
                )
```

- [ ] **Step 4: Run to verify it passes + full server suite**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): register operate tool + dispatch"
```

---

## Task 8: surface operability in `list_sources`

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (`_SOURCES` entries for sources that carry tabular files: zenodo, datacite, huggingface, dataone)
- Test: `tests/test_server.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py  (append)
@pytest.mark.asyncio
async def test_list_sources_advertises_operable():
    out = await server._dispatch("list_sources", {})
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["zenodo"].get("operable") is True
    # a discovery-only / non-tabular source stays false/absent
    assert by_name["omicsdi"].get("operable") in (False, None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_server.py -k operable -v`
Expected: FAIL (`operable` key absent).

- [ ] **Step 3: Add `"operable": True` to the tabular-bearing `_SOURCES` entries**

Read the `_SOURCES` list and add `"operable": True,` to the `zenodo`, `datacite`, `huggingface`, and `dataone` dict entries (next to their existing `"fetchable"` key). Leave the others without the key.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_server.py -k operable -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): advertise operable sources in list_sources"
```

---

## Task 9: schema-gate coverage for `access_modes`

**Files:**

- Modify: `tests/test_output_schema_gate.py` (`_sample_resource`)
- Test: same file

- [ ] **Step 1: Add `access_modes` to the representative resource**

In `_sample_resource()` (`tests/test_output_schema_gate.py:33`), add to the `DataResource(...)` constructor:

```python
        access_modes=["fetch", "schema", "preview", "head", "sql"],
```

- [ ] **Step 2: Run the gate**

Run: `uv run pytest tests/test_output_schema_gate.py -v`
Expected: PASS (the new field round-trips through `model_dump(mode="json")` against the schema).

- [ ] **Step 3: Commit**

```bash
git add tests/test_output_schema_gate.py
git commit -m "test(gate): exercise access_modes in the output-schema gate"
```

---

## Task 10: live real-execution test (remote Parquet)

**Files:**

- Test: `tests/test_operate.py` (append)

- [ ] **Step 1: Add the live test**

```python
# tests/test_operate.py  (append)
import os

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
    from data_aggregator_mcp.models import DataResource, FileEntry

    res = DataResource(
        id="hf:live", source="huggingface", kind="dataset", title="t",
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
```

- [ ] **Step 2: Run it live**

Run: `DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest tests/test_operate.py -k live -v`
Expected: PASS — schema reads the footer; `sql` returns 5 rows via range reads (DuckDB httpfs), no full download.

If the chosen URL is unavailable, swap to any other public Parquet (verify with a quick `curl -sI` first) and keep the assertions.

- [ ] **Step 3: Commit**

```bash
git add tests/test_operate.py
git commit -m "test(operate): live remote-Parquet real-execution test"
```

---

## Task 11: README + CHANGELOG + version bump to 0.19.0

**Files:**

- Modify: `pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, `server.json` (×2), `CHANGELOG.md`, `README.md`, `tests/test_packaging.py`

- [ ] **Step 1: Update the version test first**

In `tests/test_packaging.py`, rename `test_version_is_0180_and_synced` → `test_version_is_0190_and_synced` and change the expected value to `0.19.0` (read the file first; also update the `"0.18.0"` literal in `test_server_json_matches_package_identity` if present).

Run: `uv run pytest tests/test_packaging.py -k version -v` → FAIL (expected 0.19.0, got 0.18.0).

- [ ] **Step 2: Bump all four sites to `0.19.0`**

`pyproject.toml` `version`, `src/data_aggregator_mcp/__init__.py` `__version__`, `server.json` top-level `version` and `packages[0].version`.

- [ ] **Step 3: CHANGELOG `[0.19.0]`** (insert above `## [0.18.0]`):

```markdown
## [0.19.0] - 2026-06-01

### Added

- **`operate` tool (5th tool)** — inspect/query a remote tabular file (Parquet/CSV/TSV) without downloading it: `op="schema"` (columns+types), `"preview"` (sample), `"head"` (first n rows), `"sql"` (read-only SELECT against the file as the view `data`). Addresses a file by catalog id + file name. Requires the optional `[operate]` extra (`duckdb`/`pyarrow`/`fsspec`); the base install is unchanged.
- **`DataResource.access_modes`** — best-effort capability claim (`fetch` + operate modes), populated on `resolve`, degrading to `["fetch"]` when the `[operate]` extra is absent; `list_sources` flags `operable` sources.

### Security

- `operate(op="sql")` runs user SQL in a locked-down DuckDB: read-only, `disabled_filesystems='LocalFileSystem'` (httpfs only), `lock_configuration`, single-SELECT validation, plus row/byte/wall-clock caps.
```

- [ ] **Step 4: README** — add `operate` to the tools list and a short bullet under "Why this" (operate-on-data-in-place). Add an install note: `uvx data-aggregator-mcp` for base; `pip install data-aggregator-mcp[operate]` for operate.

- [ ] **Step 5: Full suite + lint, then commit**

Run: `uv run pytest -q -k "not live" && uv run ruff check src tests`
Expected: all green.

```bash
git add -A
git commit -m "chore: bump to 0.19.0 (operate-on-data tabular core)"
```

---

## Final verification (after all tasks)

- [ ] **Offline suite + lint:** `uv run pytest -q -k "not live" && uv run ruff check src tests` → green.
- [ ] **Base-install guard:** in a venv WITHOUT the extra, `python -c "import data_aggregator_mcp.server"` imports cleanly and `operate` returns the install-the-extra error (not an ImportError crash).
- [ ] **Live:** `DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest tests/test_operate.py tests/test_tabular.py tests/test_duckquery.py -k live -v` → green.
- [ ] Then **superpowers:finishing-a-development-branch** to merge `operate-on-data` → `main` (local, `--no-ff`, v0.19.0).

## Self-Review notes (author)

- **Spec coverage:** ops schema/preview/head/sql (T4/T5/T6/T7); access_modes two-tier (T2 claim, T6 Tier-2 verify via `_select_file`/`_operable` + engine failure); id+file addressing (T6 `_select_file`); SQL hardening + security test (T5); resource limits (T5 row cap, T6 wall-clock; byte cap via row cap + LIMIT — NOTE: result-byte cap is approximated by the row cap in this wave; a hard serialized-byte cap is deferred and called out here); `[operate]` extra + degrade (T1/T2/T3/T6); list_sources (T8); schema gate (T9); live test (T10); version (T11).
- **Type consistency:** engines return `{columns:[{name,type}], rows:[{}], ...}`; `operate.run` adds `file`+`op`; `tabular.schema` returns `{format, columns, row_estimate}` (no `rows`), `preview`/`head`/`sql` include `rows`. `derive_access_modes(files, *, operate: bool)` signature identical in T2 and T3.
- **Deferred (explicit):** htsget `region`; HF datasets-server; raw-URL addressing; a hard serialized result-byte cap (row cap stands in this wave); CSV source-size ceiling enforcement is light (range-read sniff only reads the first 64 KB for schema/preview; DuckDB CSV `sql`/`head` over a huge CSV is bounded by the LIMIT but still streams — a real `CSV_SOURCE_CEILING` HEAD check can be added in T6 if a live CSV case warrants it).
