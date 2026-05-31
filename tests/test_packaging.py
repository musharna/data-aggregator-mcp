from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import data_aggregator_mcp

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())


def test_version_is_0160_and_synced() -> None:
    assert data_aggregator_mcp.__version__ == "0.16.0"
    assert _PYPROJECT["project"]["version"] == "0.16.0"


def test_pyproject_has_urls_and_keywords() -> None:
    proj = _PYPROJECT["project"]
    urls = proj["urls"]
    assert urls["Repository"] == "https://github.com/musharna/data-aggregator-mcp"
    assert "Homepage" in urls and "Issues" in urls
    assert "mcp" in proj["keywords"]
    assert "model-context-protocol" in proj["keywords"]


def test_classifier_is_beta() -> None:
    assert "Development Status :: 4 - Beta" in _PYPROJECT["project"]["classifiers"]


def test_license_is_spdx_with_file_and_no_classifier() -> None:
    proj = _PYPROJECT["project"]
    # PEP 639 SPDX string form (not the deprecated `{ text = ... }` table).
    assert proj["license"] == "MIT"
    assert proj["license-files"] == ["LICENSE"]
    assert (_ROOT / "LICENSE").is_file()
    # PyPI hard-rejects a License-Expression alongside a license trove classifier.
    assert not any(c.startswith("License ::") for c in proj["classifiers"])


def test_server_json_matches_package_identity() -> None:
    sj = json.loads((_ROOT / "server.json").read_text())
    assert sj["name"] == "io.github.musharna/data-aggregator-mcp"
    assert sj["version"] == "0.16.0"
    assert sj["$schema"].endswith("/server.schema.json")
    pkg = sj["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == _PYPROJECT["project"]["name"]  # "data-aggregator-mcp"
    assert pkg["version"] == data_aggregator_mcp.__version__
    assert pkg["transport"] == {"type": "stdio"}
    assert pkg["runtimeHint"] == "uvx"


def test_readme_has_ownership_marker_matching_server_name() -> None:
    sj = json.loads((_ROOT / "server.json").read_text())
    readme = (_ROOT / "README.md").read_text()
    marker = re.search(r"^mcp-name:\s*(\S+)\s*$", readme, re.MULTILINE)
    assert marker is not None, "README must carry the mcp-name ownership marker"
    assert marker.group(1) == sj["name"]


def test_publish_runbook_covers_the_four_gate_steps() -> None:
    text = (_ROOT / "PUBLISH.md").read_text().lower()
    assert "gh repo create" in text  # 1. public repo
    assert "trusted publisher" in text  # 2. PyPI pending publisher
    assert "gh release create" in text  # 3. cut release -> publish
    assert "mcp-publisher publish" in text  # 4. registry submission
