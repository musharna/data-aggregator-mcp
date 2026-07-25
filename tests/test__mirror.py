"""Contract for the record-identity module extracted from the router (O4).

The behavior itself is covered by tests/test_content_dedup.py and tests/test_router.py,
which reach it through router's historical names — these tests pin the split so those
stay meaningful.
"""

from __future__ import annotations

import ast
from pathlib import Path

from data_aggregator_mcp import _mirror, router, sources


def test_router_reexports_are_the_extracted_objects():
    """router keeps the historical private names, but they ARE the _mirror objects —
    no copy that could drift, and the existing tests keep testing live code."""
    assert router._dedup is _mirror.dedup_by_doi
    assert router._collapse_mirrors is _mirror.collapse_mirrors
    assert router._fetch_priority is _mirror.fetch_priority
    assert router._fingerprint_key is _mirror.fingerprint_key
    assert router._checksums is _mirror.checksums
    assert router._normalize_title is _mirror.normalize_title
    assert router._first_author_surname is _mirror.first_author_surname
    assert router._survivor_rank is _mirror.survivor_rank
    assert router._DISCOVERY_ONLY_SOURCES is _mirror.DISCOVERY_ONLY_SOURCES


def test_discovery_only_set_still_derives_from_the_registry():
    """Precedence must not become a second hand-maintained list — that restatement is
    exactly what the central registry was introduced to kill."""
    assert _mirror.DISCOVERY_ONLY_SOURCES is sources.DISCOVERY_ONLY


def test_mirror_does_not_import_the_router():
    """The dependency runs one way: the orchestrator calls into this policy, never the
    reverse. An import back would reintroduce the cycle the split removes."""
    tree = ast.parse(Path(_mirror.__file__ or "").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m.endswith("router")}, sorted(imported)


def test_module_is_pure_no_client_or_http():
    """Identity decisions are deterministic and offline; an HTTP dependency here would
    mean dedup could fail or vary per run."""
    src = Path(_mirror.__file__ or "").read_text()
    assert "httpx" not in src
    assert "async def" not in src
