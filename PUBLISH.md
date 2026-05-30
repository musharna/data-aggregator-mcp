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

Set the version in **all three** places to the release value —
`pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, and `server.json`
(both top-level `version` and `packages[0].version`). The tree is already at
`0.11.0` for the first release, so no bump is needed there — just tag. Then:

```bash
git tag v0.11.0
git push origin main --tags
gh release create v0.11.0 --title v0.11.0 --notes-from-tag
```

The publish workflow verifies the tag matches the package version, builds the
wheel + sdist, and uploads to PyPI via OIDC trusted publishing.

### 4. Submit to the official MCP registry (after the PyPI release is live)

```bash
# install the publisher CLI (see modelcontextprotocol/registry releases)
mcp-publisher login github      # OIDC device flow; grants the io.github.musharna/* namespace
mcp-publisher publish           # reads server.json; validates the README mcp-name marker
```

The registry fetches `https://pypi.org/pypi/data-aggregator-mcp/json` and
confirms the `mcp-name: io.github.musharna/data-aggregator-mcp` marker is present
in the published description — so the PyPI release in step 3 must land first.

## Future enhancement (not built)

Registry submission can be automated in GitHub Actions via OIDC (a separate
`mcp-publisher` CI step). Left manual here by design.
