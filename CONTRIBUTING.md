# Contributing

Thanks for helping improve `data-aggregator-mcp`. This is an MCP server that
finds and fetches research data across archives (Zenodo, DataCite), omics
registries (NCBI, OmicsDI, DataONE), literature (PubMed/OpenAIRE), and
HuggingFace, behind five tools (`search`, `resolve`, `fetch`, `list_sources`,
`operate`) and one normalized `DataResource` model.

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
- Add or update tests for the behavior you change. New source backends and tool
  behavior should come with mocked-API tests; live probes (gated by
  `DATA_AGGREGATOR_MCP_LIVE`) are welcome for new sources.
- Keep changes fail-loud: surface per-source failures in `errors{}` rather than
  silently dropping results, and don't add silent fallbacks.
- Update `CHANGELOG.md` for any user-facing change, and the README/docs if you
  change tool signatures, sources, or configuration.
- Link the issue your PR addresses, if there is one.
