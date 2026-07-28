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

Set the version in **all five** places to the release value:

| file                                  | field                                |
| ------------------------------------- | ------------------------------------ |
| `pyproject.toml`                      | `version`                            |
| `src/data_aggregator_mcp/__init__.py` | `__version__`                        |
| `server.json`                         | top-level `version`                  |
| `server.json`                         | `packages[0].version`                |
| `CITATION.cff`                        | `version` (and bump `date-released`) |

`CITATION.cff` is the newest and the easiest to forget — it arrived after this
list was written and said "four". CI enforces it (`.github/workflows/citation.yml`
fails when it disagrees with `pyproject.toml`), so a miss shows up as a red
release PR rather than a bad publish — but only if you notice before tagging.
`.zenodo.json` carries no version field; it does not need touching.

Then:

```bash
git tag -a vX.Y.Z -m vX.Y.Z     # MUST be annotated — --notes-from-tag reads the tag message
git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes-from-tag
```

`git tag -a` matters: a lightweight tag has no message, and `--notes-from-tag`
then produces empty release notes. Verify with `git cat-file -t vX.Y.Z` — it must
print `tag`, not `commit`. From a worktree where `main` is checked out elsewhere,
tag the SHA directly (`git tag -a vX.Y.Z <sha> -F notes.txt`); there is no need to
check `main` out.

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
