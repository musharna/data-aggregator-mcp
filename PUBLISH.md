# Publishing data-aggregator-mcp

Every step below is **outward-facing and irreversible** (PyPI versions cannot be
re-uploaded or deleted; the PyPI project name and the registry name are
permanent). The repo is prepared to the gate — nothing here has been executed.
Run these manually when ready to ship a release.

## One-time setup

### 1. Create the public GitHub repo

```bash
gh repo create musharna/data-aggregator-mcp --public --source=. --remote=origin --push
```

### 2. Configure PyPI trusted publisher (no token needed)

On https://pypi.org → your account → _Publishing_ → _Add a pending trusted publisher_:

- PyPI Project Name: `data-aggregator-mcp`
- Owner: `musharna`
- Repository name: `data-aggregator-mcp`
- Workflow name: `publish.yml`
- Environment name: `pypi`

The first OIDC publish (step 3) creates the project automatically.

## Per release

### 3. Cut the release (fires `.github/workflows/publish.yml`)

Set the version in **all four** places to the release value —
`pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, and `server.json`
(both top-level `version` **and** `packages[0].version`). Then:

```bash
git tag vX.Y.Z
git push origin main --tags
gh release create vX.Y.Z --title vX.Y.Z --notes-from-tag
```

The publish workflow verifies the tag matches the package version, builds the
wheel + sdist, and uploads to PyPI via OIDC trusted publishing.

### 4. Submit to the official MCP registry — automated

`.github/workflows/publish-registry.yml` handles this via GitHub Actions OIDC
(`mcp-publisher login github-oidc`) — no device flow, no stored credentials. It
fires automatically on a published release: it waits for the PyPI release to be
queryable (the registry validates `https://pypi.org/pypi/data-aggregator-mcp/json`
and the `mcp-name: io.github.musharna/data-aggregator-mcp` marker in the
published description), then runs `mcp-publisher publish` reading `server.json`.

To (re)publish the current `server.json` version without cutting a release —
e.g. to backfill a release whose registry step predated this workflow — trigger
it manually:

```bash
gh workflow run publish-registry.yml --ref main
```

The manual device-flow path (`mcp-publisher login github`) remains available as
a fallback but is not needed; mcp-publisher 1.7.9's device flow does not honor
GitHub's poll interval and reliably fails with `slow_down`.
