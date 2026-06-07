# Security Policy

## Supported versions

`data-aggregator-mcp` ships fixes against the latest released version only.
The current release is **v0.20.0**. Please reproduce any issue on the latest
release (`uvx data-aggregator-mcp` always pulls it) before reporting.

| Version         | Supported          |
| --------------- | ------------------ |
| latest (0.20.x) | :white_check_mark: |
| < latest        | :x:                |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report privately, either way:

- Preferred: use GitHub's **"Report a vulnerability"** button under the repo's
  **Security** tab (private security advisories), or
- Email **mjarnold1998@gmail.com**.

Please include a description of the issue, the affected version, and a minimal
reproduction (the `search`/`resolve`/`fetch`/`operate`/`list_sources` call, the
data source involved, and any input you fed it). You can expect an initial
acknowledgement within a few days. Once a fix ships, you'll be credited in the
release notes unless you ask otherwise.

## Security model

This server reaches out to remote services and brings remote data onto the
machine it runs on. Two parts of the surface are worth understanding before you
deploy it:

- **`fetch` downloads remote files to disk.** Files are streamed under a
  `max_bytes` guard and md5/sha-256 verified **where the source exposes a
  checksum** (Zenodo, SRA, DataONE, Figshare/Dataverse/OSF). Sources without an
  upstream checksum (GEO `suppl/`, literature full text, some OmicsDI repos) get
  a content-type sniff that fails loud on an HTML page served in place of a
  binary, but the bytes themselves are not cryptographically verified. Archive
  extraction (`extract=True`, off by default) is guarded against path traversal
  and runaway extracted size. Treat fetched content as untrusted input and point
  `dest` at a sandboxed location.

- **The `operate` tool executes user-supplied SQL.** `operate(op="sql", ...)`
  runs the query in a locked-down DuckDB instance: read-only, local filesystem
  access disabled, single-`SELECT` validation, and row / wall-clock caps. It
  reads remote Parquet/CSV/TSV over `httpfs` range reads. Even so, SQL and the
  remote URLs it touches should come from trusted inputs — run it against data
  sources and queries you trust, and do not expose it to fully untrusted callers
  without an additional sandbox boundary.

If you believe either guard can be bypassed (checksum/content-type bypass, a SQL
escape from the DuckDB sandbox, or a path-traversal in extraction), please
report it privately using the channels above.
