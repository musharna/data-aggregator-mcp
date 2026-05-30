from __future__ import annotations

from pathlib import Path

import yaml

_WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_ci_workflow_parses_and_runs_tests() -> None:
    ci = yaml.safe_load((_WF / "ci.yml").read_text())
    # PyYAML parses the bare `on:` key as boolean True — accept either form.
    triggers = ci.get("on", ci.get(True))
    assert "push" in triggers and "pull_request" in triggers
    job = ci["jobs"]["test"]
    versions = job["strategy"]["matrix"]["python-version"]
    assert "3.11" in versions and "3.12" in versions
    run_steps = " ".join(s.get("run", "") for s in job["steps"])
    assert "ruff check" in run_steps
    assert "pytest" in run_steps


def test_publish_workflow_uses_oidc_on_release() -> None:
    pub = yaml.safe_load((_WF / "publish.yml").read_text())
    triggers = pub.get("on", pub.get(True))
    assert "release" in triggers
    assert triggers["release"]["types"] == ["published"]
    publish_job = pub["jobs"]["pypi-publish"]
    # OIDC trusted publishing: id-token write permission, no stored token
    assert publish_job["permissions"]["id-token"] == "write"
    assert publish_job["environment"] == "pypi"
    uses = " ".join(s.get("uses", "") for s in publish_job["steps"])
    assert "pypa/gh-action-pypi-publish" in uses


def test_publish_workflow_guards_tag_version_match() -> None:
    pub = yaml.safe_load((_WF / "publish.yml").read_text())
    run_steps = " ".join(s.get("run", "") for s in pub["jobs"]["build"]["steps"])
    assert "pyproject.toml" in run_steps  # version-vs-tag guard reads the version
