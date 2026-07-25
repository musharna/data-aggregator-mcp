# Contributing

Thanks for helping improve `data-aggregator-mcp`. This is an MCP server that
finds and fetches research data across archives (Zenodo, DataCite), omics
registries (NCBI, OmicsDI, DataONE), literature (PubMed/OpenAIRE), and
HuggingFace, behind six tools (`search`, `resolve`, `fetch`, `list_sources`,
`operate`, `relate`) and one normalized `DataResource` model.

## Dev setup

Requires Python ≥3.11 and [`uv`](https://docs.astral.sh/uv/). Install with the
`dev` and `operate` extras (the `operate` tests need DuckDB/pyarrow, so install
both to match CI):

```bash
uv venv
uv pip install -e ".[dev,operate]"
```

(or `uv sync --extra dev --extra operate`, which is what CI runs.)

## Running the tests

```bash
uv run pytest -q
```

Tests are network-free by default (`pytest-httpx` mocks). The live API probes
are gated behind an env var and are not run in CI:

```bash
DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -k live -q   # hits real APIs
```

## Testing rules

Three rules, each learned here the expensive way. All three are the same idea:
**a test is worth only what it can fail on.**

### 1. A mocked test cannot validate a request format

`pytest-httpx` mocks are the right tool for behavior _given_ a response — parsing,
fail-soft paths, pagination, error mapping. They cannot establish that an upstream
accepts the request we send, because the mock's matcher and the code's format come
from the same assumption. Such a test passes whether or not that assumption holds.

Not hypothetical: `fulltext.py` asked EuropePMC for a phrase-quoted
`PMCID:"PMC3463246"`, a form that matches nothing there, so every open-access
paper we knew a PMCID for was reported paywalled. Six mocked tests covered the
path. All six passed, and one of them asserted the broken quoting was correct.

So: **every upstream the code can reach needs at least one live test showing the
real server accepts our request** — not a mock asserting we produce the string we
believe it wants. Live tests are gated behind `DATA_AGGREGATOR_MCP_LIVE` and
skipped in CI, which is what makes them cheap to add.

Find the gaps mechanically by recording what the suite really sends:

```bash
PYTHONPATH=. DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -p tests.reqrec -q
```

Any upstream absent from that output is mock-only. See `tests/reqrec.py`, which
also explains why it hooks the transport rather than the client.

### 2. Never trust a test you have not seen fail

A test that has only ever passed is unvalidated: you know that it passes, not
that it is wired to the thing it claims to guard. Run it against the broken state
first — for a bug fix that is the pre-fix code, which you still have — and confirm
it fails, and fails for the stated reason.

This is not ceremony. The first regression test for the `operate` SSRF fix passed
against the _vulnerable_ code: it used a `file://` source, so the attack failed
for an unrelated reason (`LocalFileSystem` was already disabled) and the test
asserted only that _some_ exception occurred. It would have shipped as proof of a
fix it never exercised. Treat this as mandatory for security tests.

### 3. A negative result needs a positive control

"Nothing bad happened" means nothing unless the normal path demonstrably works in
the same harness. The first SSRF probe reported every case cleanly blocked — but
only because the stub HTTP server implemented `GET` and not `HEAD`/Range, so
DuckDB never loaded the source and no case ever reached the security boundary.

Assert the legitimate query still succeeds inside the same test that asserts the
attack fails. `tests/test_duckquery.py::test_user_sql_cannot_reach_the_network`
is the worked example.

## Linting

CI runs Ruff over the whole tree; match it locally before pushing:

```bash
uv run ruff check .
```

Line length is 100, target version py311 (see `[tool.ruff]` in
`pyproject.toml`).

## Running the server locally

The server speaks MCP over stdio:

```bash
uvx data-aggregator-mcp
# or, from a checkout:
uv run data-aggregator-mcp        # = python -m data_aggregator_mcp
```

Register it with a client (e.g. Claude Code):

```bash
claude mcp add data-aggregator -- uvx data-aggregator-mcp
```

## Pull requests

- `uv run ruff check .` and `uv run pytest -q` must both pass — CI enforces this
  on Python 3.11, 3.12, and 3.13.
- Add or update tests for the behavior you change, following the three rules in
  [Testing rules](#testing-rules). New source backends and tool behavior come with
  mocked-API tests, plus at least one live probe (gated by
  `DATA_AGGREGATOR_MCP_LIVE`) for any request format a real server has to accept.
- Keep changes fail-loud: surface per-source failures in `errors{}` rather than
  silently dropping results, and don't add silent fallbacks.
- Update `CHANGELOG.md` for any user-facing change, and the README/docs if you
  change tool signatures, sources, or configuration.
- Link the issue your PR addresses, if there is one.
