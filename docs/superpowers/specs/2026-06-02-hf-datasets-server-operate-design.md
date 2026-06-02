# HF datasets-server backend for `operate` — Design

**Date:** 2026-06-02
**Status:** Approved (brainstorming) → writing-plans
**Scope:** Phase 2 "#2a" operate follow-up. Ships **v0.20.0**.
**Supersedes/extends:** `2026-06-01-operate-on-data-design.md` (v0.19.0 operate wave).

## Goal

Make **any** HuggingFace dataset operable (`schema`/`preview`/`head`/`sql`), not
just ones that happen to store `.parquet`/`.csv` at the raw
`huggingface.co/datasets/<id>/resolve/main/<file>` URL. We do this by surfacing
the HuggingFace **datasets-server** auto-converted Parquet files as ordinary
`FileEntry`s — which the _existing_ operate engines already handle unchanged.

## Background — current state (grounded, read 2026-06-02)

- `operate.run()` (`operate.py:99`) resolves a `resource_id` via `router.resolve`,
  picks an operable file with `_select_file`/`_operable` (`operate.py:38` — a file
  is operable iff it has a `url` and a name ending in `.parquet/.pq/.csv/.tsv`),
  then dispatches: `schema`/`preview` → `tabular.py` (pyarrow footer / CSV sniff,
  range reads); `head`/`sql` → `duckquery.py` (DuckDB+httpfs, size-gated).
- `huggingface.resolve()` (`huggingface.py:87`) builds one `FileEntry` per raw
  repo sibling: `url=f"{FILE_BASE}/{ds_id}/resolve/main/{rfilename}"`
  (`huggingface.py:44`). Datasets stored as JSON/JSONL/arrow/loader-scripts thus
  expose **no** operable file — `operate` fails loud ("no operable tabular file").
- `derive_access_modes(files, *, operate)` (`models.py:177`) advertises the four
  operate modes whenever `files[]` contains a tabular-extension file and the
  `[operate]` extra is installed. It is purely file-driven.
- The `huggingface` entry in `list_sources` already carries `fetchable` +
  `operable` (set in the v0.19.0 wave). No change needed there.

### Why htsget is NOT in this slice

Grepped all of `src/`: **no source emits htsget/genomic endpoints**; every
`FileEntry.url` is a plain download URL and `operate` takes a `resource_id`
(resolved to `files[].url`), not a raw URL. htsget is a _server protocol_ and no
current source (Zenodo/DataCite/OmicsDI/DataONE/HF/PRIDE/MetaboLights) serves it.
htsget would first require a genomic-source adapter + heavy deps (pysam/htsget
client) for a narrow audience — deferred to a future genomic-source wave.

### Live API findings (probed 2026-06-02)

`GET https://datasets-server.huggingface.co/parquet?dataset=<id>` returns
`{"parquet_files": [{config, split, url, size, ...}], "partial": bool}`. Each
`url` has the form
`https://huggingface.co/datasets/<id>/resolve/refs%2Fconvert%2Fparquet/<config>/<split>/<n>.parquet`
— **the exact URL format the v0.19.0 live operate test already proved** DuckDB

- httpfs and the pyarrow footer can read (302→CDN, `accept-ranges: bytes`).
  So the converted Parquet is operable by the existing engines with zero new code.

## Architecture — Approach A: enrich `resolve()` with converted Parquet

The datasets-server view is surfaced as additional `FileEntry`s during HF
`resolve()`. `operate`, `access_modes`, and the engines are **untouched**; the
new capability falls out of the existing file-driven machinery.

Rejected alternatives: **B** (dedicated datasets-server backend via
`/first-rows`+`/rows`) adds a new module, `config`/`split` addressing, and gives
no `sql` — strictly weaker. **C** (lazy: `operate` consults datasets-server only
when no operable raw file) puts a source-specific branch in `operate.run` and
makes `access_modes` under-claim at resolve time, defeating the two-tier
capability-claim design.

## Components

### 1. New module `src/data_aggregator_mcp/hf_datasets_server.py`

```
DSS_API = "https://datasets-server.huggingface.co"
MAX_DSS_FILES = 100

async def parquet_files(client, ds_id) -> list[FileEntry]:
    # GET {DSS_API}/parquet?dataset={ds_id} via _http.request_json
    # for each p in body["parquet_files"]:
    #   FileEntry(
    #     name = f"{p['config']}/{p['split']}/{p['url'].rsplit('/', 1)[-1]}",
    #     url  = p["url"],
    #     size = p.get("size"),
    #     source = "hf-datasets-server",
    #   )
    # cap to MAX_DSS_FILES; if len(parquet_files) > MAX_DSS_FILES:
    #   log.warning("datasets-server: %s has %d parquet files; capping to %d",
    #               ds_id, n, MAX_DSS_FILES)  -> stderr, NOT silent
```

A module-level `logging.getLogger(__name__)` is the warn channel (stderr;
surfaced in MCP server logs). Follows the repo's existing logging usage (confirm
convention at build time; if none exists, use `logging` stdlib directly — no new
dep).

### 2. `huggingface.resolve()` integration

After `_normalize(body)` builds the raw-sibling resource, append the converted
Parquet files **best-effort**:

```
resource = _normalize(body)
try:
    resource.files += await hf_datasets_server.parquet_files(client, ds_id)
except NotFoundError:
    pass  # no converted view — normal (gated / too-big / non-tabular / pending)
except Exception as exc:
    log.warning("datasets-server enrichment failed for %s: %r", ds_id, exc)
return resource
```

**Fail-soft policy (explicit):** discovery resolves must not break because
datasets-server lacks a conversion or has a transient outage — the raw siblings
remain the source of truth. A 404 is a _normal_ "no converted view" signal and is
swallowed silently. Any other error is swallowed **but logged with full context**
to stderr (debuggable, not disguised) — consistent with the repo's existing
best-effort enrichment (citation/license/croissant render fail-soft).

### 3. `access_modes` — no change

`derive_access_modes` is file-driven; once converted Parquet entries are in
`files[]`, schema/preview/head/sql are advertised automatically. The two-tier
claim becomes honest for the whole HF source.

### 4. `operate` / engines / `list_sources` — no change

Existing `_select_file`/`_operable`/`tabular.py`/`duckquery.py` handle the new
URLs. Multi-split datasets surface the existing "record has multiple operable
files; pass file=<name>" UX. `list_sources` already flags `huggingface` operable.

## Data flow

```
operate(id="hf:<ds>", op="sql", query=...)
  └─ router.resolve → huggingface.resolve
       ├─ raw siblings  → FileEntry(resolve/main/...)         (as today)
       └─ parquet_files → FileEntry(refs/convert/parquet/...) (NEW, best-effort)
  └─ _select_file picks an operable .parquet (or file= disambiguates)
  └─ duckquery.run_sql over httpfs  → rows   (existing, verified engine)
```

## Error handling

| Condition                                    | Behavior                                         |
| -------------------------------------------- | ------------------------------------------------ |
| datasets-server has no conversion (404)      | Skip enrichment silently; raw siblings only      |
| datasets-server 5xx / timeout / network      | Skip enrichment; **log full error to stderr**    |
| `> MAX_DSS_FILES` parquet files              | Truncate to 100; **log dropped count to stderr** |
| resolved record has no operable file         | `operate` fails loud (existing message)          |
| converted Parquet unreadable at operate time | existing engine fails loud per-file              |

## Testing

**Unit (mocked):**

- `parquet_files`: mapping (name/url/size/source) from a fixture `/parquet` body;
  empty `parquet_files`; cap+warn when `> MAX_DSS_FILES`; 404 → propagates
  `NotFoundError` (caller decides).
- `huggingface.resolve` enrichment: monkeypatch `parquet_files` to return entries
  → assert appended after raw siblings; monkeypatch to raise `Exception` →
  resolve still returns raw siblings AND a warning is logged (caplog); monkeypatch
  to raise `NotFoundError` → raw siblings, no warning.
- `access_modes`: an HF-shaped resource with a converted-Parquet `FileEntry`
  derives `[fetch, schema, preview, head, sql]`.

**Real-execution check (boundary, `DATA_AGGREGATOR_MCP_LIVE=1`):**

- Resolve a real small public HF dataset → assert ≥1 `source="hf-datasets-server"`
  Parquet `FileEntry` is present → run `operate schema` (and one `sql`) end-to-end
  on it against the live datasets-server + HF CDN.

## Release

- Bump `0.19.0 → 0.20.0` in `pyproject.toml`, `src/data_aggregator_mcp/__init__.py`,
  `server.json` (top-level + `packages[0].version`).
- `CHANGELOG.md`: `## [0.20.0]` — Added: HF datasets operable via datasets-server
  auto-converted Parquet.
- `README.md`: note under operate / HuggingFace that HF datasets gain
  schema/preview/head/sql via the datasets-server converted Parquet.
- Update `test_packaging.py` version assertions to `0.20.0`.

## Out of scope (YAGNI)

Gated/private-dataset auth; the `/first-rows` and `/rows` REST paths (converted
Parquet + existing engines already cover all four ops); `partial`-conversion
flagging; `operate` consuming `FileEntry.size` to skip the size-gate HEAD;
htsget (separate future wave).
