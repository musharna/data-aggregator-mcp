<!-- Thanks for contributing to data-aggregator-mcp! -->

## What does this PR do?

<!-- A short description of the change and the motivation. Link any related issue. -->

Fixes #

## Checklist

- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest -q` passes
- [ ] Added/updated mocked-API tests for the change (and live probes, gated by `DATA_AGGREGATOR_MCP_LIVE`, for new sources)
- [ ] Fail-loud: per-source failures surface in `errors{}` rather than being silently dropped; no silent fallbacks added
- [ ] Updated `CHANGELOG.md` for any user-facing change
- [ ] Updated README/docs if tool signatures, sources, or configuration changed
