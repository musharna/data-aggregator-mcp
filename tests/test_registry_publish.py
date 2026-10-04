"""scripts/registry_publish.sh: publish to the MCP registry, retrying while it cannot see
the PyPI release yet.

v0.58.0 (2026-10-04): "Publish to MCP Registry" failed twice, about 4 minutes apart,
with the registry's 404 for the new PyPI version, while the workflow's own wait on
pypi.org/pypi/<pkg>/<version>/json had already seen 200. The third attempt passed. The
script retries on the registry's own answer instead, and only on answers the registry
calls transient (strings from internal/validators/registries/pypi.go).
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import pytest

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "registry_publish.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-registry.yml"

# Verbatim from run 37186743913, attempts 1 and 2 (2026-10-04).
NOT_YET = (
    "Publishing to https://registry.modelcontextprotocol.io...\n"
    'Error: publish failed: server returned status 400: {"title":"Bad Request","status":400,'
    '"detail":"Failed to publish server","errors":[{"message":"registry validation failed '
    "for package 0 (data-aggregator-mcp): PyPI package 'data-aggregator-mcp' exists, but "
    "version '0.58.0' was not found (status: 404). A newly published release can take a "
    "moment to appear on PyPI. Wait and retry, or publish version '0.58.0' before "
    'registering it"}]}'
)
RATE_LIMITED = (
    'Error: publish failed: server returned status 400: {"errors":[{"message":"PyPI '
    "rate-limited the metadata request for package 'data-aggregator-mcp' (status: 429). "
    'Likely transient, retry later"}]}'
)
NO_SUCH_PACKAGE = (
    'Error: publish failed: server returned status 400: {"errors":[{"message":"PyPI '
    "package 'data-aggregator-mcp' not found (status: 404)\"}]}"
)
OK = "Publishing to https://registry.modelcontextprotocol.io...\n✓ Successfully published"

_FAKE = """#!/usr/bin/env bash
echo "$*" >>"$LOG"
case "$1" in
login) exit "${LOGIN_RC:-0}" ;;
publish)
	n=$(grep -c '^publish' "$LOG")
	if [ -f "$ANSWERS/$n.rc" ]; then cat "$ANSWERS/$n.out"; exit "$(cat "$ANSWERS/$n.rc")"; fi
	echo "no answer queued for publish $n"; exit 99 ;;
esac
"""


class _Publisher:
    """A fake mcp-publisher answering each publish from a queue; logs every call."""

    def __init__(self, tmp: Path) -> None:
        self.bin, self.log, self.answers = tmp / "mcp-publisher", tmp / "calls", tmp / "answers"
        self.bin.write_text(_FAKE)
        self.bin.chmod(0o755)
        self.answers.mkdir()
        self.queued = 0

    def answer(self, out: str, rc: int) -> _Publisher:
        self.queued += 1
        (self.answers / f"{self.queued}.out").write_text(out)
        (self.answers / f"{self.queued}.rc").write_text(str(rc))
        return self

    def run(self, *login: str, tries: int = 5, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.bin), *(login or ("github-oidc",))],
            env={
                **os.environ,
                "LOG": str(self.log),
                "ANSWERS": str(self.answers),
                "TRIES": str(tries),
                "WAIT": "0",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def test_the_registry_not_yet_seeing_pypi_is_retried_until_it_does(tmp_path: Path) -> None:
    pub = _Publisher(tmp_path).answer(NOT_YET, 1).answer(NOT_YET, 1).answer(OK, 0)
    result = pub.run()
    assert result.returncode == 0, result.stderr
    assert pub.calls == ["login github-oidc", "publish"] * 3  # a fresh token every try
    assert "try 2/5" in result.stdout and "try 3/5" in result.stdout
    # positive control: published at once, one try
    (tmp_path / "once").mkdir()
    once = _Publisher(tmp_path / "once").answer(OK, 0)
    assert once.run().returncode == 0 and once.calls == ["login github-oidc", "publish"]


def test_a_rate_limit_the_registry_calls_transient_is_retried(tmp_path: Path) -> None:
    pub = _Publisher(tmp_path).answer(RATE_LIMITED, 1).answer(OK, 0)
    assert pub.run().returncode == 0
    assert pub.calls.count("publish") == 2


def test_any_other_failure_is_final(tmp_path: Path) -> None:
    pub = _Publisher(tmp_path).answer(NO_SUCH_PACKAGE, 3).answer(OK, 0)
    result = pub.run()
    assert result.returncode == 3  # publisher's own exit status
    assert pub.calls == ["login github-oidc", "publish"]
    assert "not a propagation delay: not retrying" in result.stderr
    assert NO_SUCH_PACKAGE in result.stdout  # the registry's answer is shown
    # positive control: the same queue led by the transient answer is retried
    (tmp_path / "b").mkdir()
    pub = _Publisher(tmp_path / "b").answer(NOT_YET, 1).answer(OK, 0)
    assert pub.run().returncode == 0 and pub.calls.count("publish") == 2


def test_it_gives_up_after_its_tries(tmp_path: Path) -> None:
    pub = _Publisher(tmp_path)
    for _ in range(3):
        pub.answer(NOT_YET, 1)
    result = pub.run(tries=3)
    assert result.returncode == 1
    assert pub.calls.count("publish") == 3
    assert "still could not see the PyPI release after 3 tries" in result.stderr


def test_a_failed_login_publishes_nothing(tmp_path: Path) -> None:
    pub = _Publisher(tmp_path).answer(OK, 0)
    result = pub.run(LOGIN_RC="7")
    assert result.returncode == 7 and pub.calls == ["login github-oidc"]
    # control: missing login method is a usage error, not a login attempt
    (tmp_path / "b").mkdir()
    bare = _Publisher(tmp_path / "b")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(bare.bin)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2 and "usage" in result.stderr and bare.calls == []


def test_the_workflow_publishes_through_the_script() -> None:
    steps = WORKFLOW.read_text()
    assert "bash scripts/registry_publish.sh ./mcp-publisher github-oidc" in steps
    assert "./mcp-publisher publish" not in steps  # no unretried publish beside it
    assert "./mcp-publisher login" not in steps  # the script logs in per try


@live_only
def test_live_the_pinned_publisher_fails_its_login_outside_actions_without_retrying(
    tmp_path: Path,
) -> None:
    """The real mcp-publisher the workflow pins: outside GitHub Actions there is no OIDC
    token, so login fails, and the script stops there with no publish attempt."""
    url = (
        "https://github.com/modelcontextprotocol/registry/releases/download/v1.7.9/"
        "mcp-publisher_linux_amd64.tar.gz"
    )
    blob = urllib.request.urlopen(url, timeout=60).read()  # noqa: S310 (pinned https URL)
    assert hashlib.sha256(blob).hexdigest() == (
        "ab128162b0616090b47cf245afe0a23f3ef08936fdce19074f5ba0a4469281ac"
    )
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extract("mcp-publisher", tmp_path, filter="data")
    binary = tmp_path / "mcp-publisher"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACTIONS_ID_TOKEN")}
    result = subprocess.run(
        ["bash", str(SCRIPT), str(binary), "github-oidc"],
        cwd=tmp_path,
        env={**env, "HOME": str(tmp_path), "WAIT": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "Publishing" not in result.stdout + result.stderr  # publish never ran
    assert "try 2/" not in result.stdout
    # control: the binary itself runs and knows both subcommands the script uses
    usage = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=30)
    assert "login" in usage.stdout + usage.stderr and "publish" in usage.stdout + usage.stderr
