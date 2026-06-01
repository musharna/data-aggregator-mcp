# Operate-on-data — first wave (tabular core) — Design

**Status:** approved-in-brainstorm 2026-06-01. Implements Phase 2 (P2.0 + P2.1 + a
tabular subset of P2.2/P2.4) of `docs/superpowers/plans/2026-05-31-one-stop-shop-master-plan.md`,
with the gap-analysis hardening notes folded in. htsget/`region` (P2.5) and the HF
datasets-server path (P2.3) are explicitly deferred to a later wave.

## Goal

Add a 5th MCP tool, `operate`, that lets an agent **inspect and query a remote
tabular file (Parquet / CSV / TSV) without downloading it** — schema, a preview,
the first N rows, or a read-only SQL SELECT — and add an `access_modes` field to
`DataResource` so an agent knows which records are operable vs locate-only.

This is the "operate-on-data-in-place" differentiator: nothing else in the MCP
research ecosystem ships it. It is all client-side and stdio-safe.

## Scope (this wave)

**In:** the `operate` tool with ops `schema`, `preview`, `head`, `sql`;
`DataResource.access_modes`; DuckDB+httpfs and pyarrow-footer/CSV-sniff engines;
the `[operate]` optional-dependency extra; resource limits + SQL hardening; offline

- security + live tests.

**Out (later waves):** `region`/htsget genomic slicing (EGA/ENA-only,
controlled-access, refuted for NCBI-SRA — low value, deferred); the HuggingFace
datasets-server REST path (HF parquet is already reachable via DuckDB+httpfs);
non-tabular formats; raw-URL addressing (catalog-id only this wave).

## Decisions (locked in brainstorm)

1. **First slice = tabular core** (DuckDB + footer). htsget deferred.
2. **Addressing = catalog id + file selector.** `operate(op, id, file=…, …)`; no raw URLs.
3. **`sql` kept, hardened DuckDB** (not dropped for structured-only ops).
4. **Version = v0.19.0** (a normal increment; 1.0 reserved for an operate-complete milestone).

## The `operate` tool

```
operate(op, id, file=None, query=None, n=20, columns=None) -> structured result
```

- **`op`** ∈ `{"schema", "preview", "head", "sql"}` (required).
- **`id`** — a `DataResource` id (e.g. `zenodo:7654321`, `datacite:10.x/y`,
  `hf:owner/name`). Resolved internally to get the file manifest + URLs.
- **`file`** — filename within the record to operate on. Optional **iff** the
  record has exactly one operable file; otherwise required. If the named file
  isn't found or isn't operable, **fail loud** listing the operable files.
- **`query`** — required for `op="sql"`; a single read-only SELECT. The remote file
  is exposed to the query as a view named `data` (e.g. `SELECT * FROM data WHERE …`).
- **`n`** — row count for `head` (default 20, hard-capped at the row cap).
- **`columns`** — optional projection for `head`/`preview`.

**Returns** a structured object (never a file path — that's `fetch`'s contract):

- `schema` → `{columns: [{name, type}], format, file}`
- `preview` → `{columns: [...], rows: [...sample...], row_estimate?, file}`
- `head` → `{columns: [...], rows: [{col: val, …}, …], file}`
- `sql` → `{columns: [...], rows: [...], truncated: bool, file}`

`rows` are JSON-serializable dicts; `truncated` is true when the row or byte cap
clipped the result.

### Op → engine mapping

| op        | engine                               | how                                                                        |
| --------- | ------------------------------------ | -------------------------------------------------------------------------- |
| `schema`  | pyarrow footer (Parquet) / CSV sniff | range-read the Parquet footer (~KB) or the first N KB of CSV; no full scan |
| `preview` | pyarrow footer + small read          | schema + a small sample (footer row-count when present)                    |
| `head`    | DuckDB + httpfs                      | `SELECT [columns] FROM data LIMIT n`                                       |
| `sql`     | DuckDB + httpfs                      | the user SELECT against the `data` view                                    |

DuckDB reads Parquet via HTTP range requests (footer + only the needed row groups —
predicate/projection pushdown). CSV has no pushdown, so it is **size-gated** (see
limits). All DuckDB/pyarrow calls are synchronous and run inside
`asyncio.to_thread` so the event loop is never blocked.

## `access_modes` (P2.0) — two-tier

Add `access_modes: list[str]` to `DataResource` (values drawn from
`{"fetch", "schema", "preview", "head", "sql"}`).

- **Tier 1 — best-effort claim** computed at resolve time from each file's
  extension/mime + source. A record with a `.parquet`/`.csv`/`.tsv` file claims the
  tabular operate modes; every fetchable record claims `"fetch"`. Because
  `compact()` strips `files[]` from search results and `mime` is frequently `None`,
  this is explicitly best-effort and extension-driven.
- **Tier 2 — verified at operate time.** `operate` does a cheap HEAD/footer probe to
  confirm the file is actually a range-readable Parquet/CSV before running the op.
  If the Tier-1 claim doesn't hold, **fail loud** (typed error) — never silently
  fall back or return empty.
- **Extra-aware:** when the `[operate]` extra is not installed, `access_modes`
  drops the operate modes and keeps only `"fetch"`, and the tool returns a clear
  "install `data-aggregator-mcp[operate]`" error.

## Safety & resource limits

`operate` needs its **own** limits — `fetch`'s `max_bytes` does not transfer to
DuckDB/fsspec.

- **Row cap** — default 1,000 rows; a hard maximum the `n`/`sql` result cannot exceed.
- **Result-byte cap** — ~5 MB serialized; over it, the result is `truncated`.
- **Timeouts** — DuckDB statement timeout + an outer wall-clock timeout (~30 s).
- **CSV source ceiling** — CSV has no pushdown; if the file exceeds a byte ceiling,
  fail loud and suggest `fetch` instead.

**SQL hardening** (the load-bearing part — `sql` runs user SQL):

- DuckDB opened **read-only**; the remote file registered as a named view `data`.
- `SET disabled_filesystems='LocalFileSystem'` — keeps `httpfs` for the remote read
  but blocks local-file reads (`read_csv('/etc/passwd')`) and `COPY … TO '<local>'`
  writes.
- `SET lock_configuration=true` after setup so the statement can't re-enable anything.
- The statement is validated to be a **single SELECT** (reject DDL/DML/PRAGMA/COPY/
  ATTACH/INSTALL/LOAD).
- A dedicated **security test** asserts a `sql` attempting a local-file read or a
  write is rejected.

## Error handling (fail-loud, per house rule)

- Unknown `op`, missing required param (`query` for `sql`), unknown `id` → typed error.
- `file` not found / not operable / ambiguous (multiple operable files, none named)
  → typed `OperateNotSupported`-style error listing the operable files.
- Tier-2 probe fails (not really Parquet/CSV, no range support) → fail loud.
- `[operate]` extra missing → clear install error.
- Engine/timeout/cap breaches → typed error or `truncated=true` (caps), never a
  silent empty result.

## Files

- `models.py` — add `access_modes: list[str]` to `DataResource`; a helper to compute
  the Tier-1 claim from `files[]` + source + extra-availability.
- `operate.py` (new) — op dispatch: resolve id → select/verify file → enforce limits
  → call engine → shape result.
- `duckquery.py` (new) — hardened DuckDB+httpfs engine for `head`/`sql`
  (read-only, disabled local FS, locked config, SELECT-only, `to_thread`).
- `tabular.py` (new) — pyarrow Parquet-footer schema + CSV sniff for `schema`/`preview`.
- `server.py` — register the 5th tool + dispatch; surface operate modes in `list_sources`.
- `router.py` — helper to compute `access_modes` per source/format if it belongs with dedup/enrich.
- `errors.py` — a not-operable / operate-unsupported typed error.
- `pyproject.toml` — `[project.optional-dependencies] operate = ["duckdb", "pyarrow", "fsspec"]`.

## Testing

- **Offline unit tests** over tiny committed Parquet + CSV fixtures: each op returns
  the expected schema/rows; file selection (single vs named vs ambiguous) behaves;
  caps clip with `truncated=true`; missing-extra path returns the install error
  (simulate by monkeypatching the import guard).
- **Security test** — a `sql` op attempting a local-file read and one attempting a
  write are both rejected.
- **Live test** (`DATA_AGGREGATOR_MCP_LIVE=1`) — `operate(op=sql)` / `op=schema`
  against a real remote Parquet (e.g. a public HuggingFace or Zenodo parquet),
  asserting filtered rows / correct columns come back **without** a full download.
  Real-execution doctrine: `operate` is a new system boundary (DuckDB↔httpfs↔remote).
- **Schema gate** — the new `access_modes` field must pass `test_output_schema_gate`
  (round-trip) and the packaging/version test bumps to 0.19.0.

## Acceptance

- `operate(op="sql", id, file, query="SELECT … WHERE …")` returns filtered rows from
  a remote Parquet without downloading the whole file.
- `operate(op="schema", id, file)` returns columns+types from the footer alone.
- `access_modes` correctly reflects per-record operability and degrades to `["fetch"]`
  when the `[operate]` extra is absent.
- A `sql` attempting local-file access is rejected; offline + security + live tests green.
- Base install (no `[operate]`) still imports and runs the other four tools.

## Version

**v0.19.0.** Tool count 4 → 5; `DataResource.access_modes` added. CHANGELOG `[0.19.0]`.
