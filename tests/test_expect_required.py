"""Every adapter reports a 200 whose JSON is the wrong type as an upstream failure.

``_http.request_json``'s ``expect`` was optional until 2026-09-30, and 37 of its 50 call
sites left it out. A caller then read the body with ``(body or {}).get(...)`` or
``body.get(...)``, so a 200 carrying ``null``, ``[]`` or a bare string became an empty
result, "no such record" (dandi, cellxgene), or an untyped ``AttributeError`` (20 entry
points). ``expect`` is now required; this sweep drives each registered adapter's
``search`` and ``resolve`` against a server that answers every request with a JSON
string, and requires the named, retried ``UpstreamUnavailableError``.
"""

from __future__ import annotations

import ast
import pathlib

import httpx
import pytest

import data_aggregator_mcp
from data_aggregator_mcp import _http, router
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests._well_formed_ids import WELL_FORMED

# Parse JSON outside request_json; the same class of miss, not covered by `expect`.
_OUTSIDE: dict[tuple[str, str], str] = {}

_CASES = [
    (name, fn)
    for name in sorted(router._ADAPTERS)
    for fn in ("search", "resolve")
    if hasattr(router._ADAPTERS[name], fn)
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


async def _call(name: str, fn: str, body: object) -> object:
    mod = router._ADAPTERS[name]
    prefix = sorted(mod.PREFIXES)[0]
    arg = "x" if fn == "search" else f"{prefix}:{WELL_FORMED.get(prefix, 'x1')}"
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as c:
        return await getattr(mod, fn)(c, arg)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "fn"),
    [
        pytest.param(
            n,
            f,
            marks=pytest.mark.xfail(strict=True, reason=_OUTSIDE[(n, f)]),
        )
        if (n, f) in _OUTSIDE
        else (n, f)
        for n, f in _CASES
    ],
)
async def test_a_wrong_type_200_is_an_upstream_failure(name: str, fn: str) -> None:
    """The failure must be the declared-type check rejecting the string body ("got str"),
    retried and named, not a 404, an AttributeError or any other error the harness could
    provoke. Positive controls for well-formed bodies are each adapter's own tests, which
    now pass through the same required ``expect``; one generic body cannot be
    well-formed here, since endpoints promise an object, a list or (PRIDE) an int."""
    with pytest.raises(UpstreamUnavailableError) as err:
        await _call(name, fn, "junk")
    assert "unparseable 200 body" in str(err.value)
    assert "got str" in str(err.value)


def test_every_request_json_call_names_expect_itself() -> None:
    """mypy enforces `expect` only where it can see it. A wrapper that forwards
    `**kwargs: Any` to request_json hides a missing `expect` from its own callers (the
    #145 review found dandi's and biostudies' `_get_json`), so every call must name
    `expect=` explicitly, and a wrapper takes it as its own required parameter.
    Positive control: the scan finds the calls it checks."""
    src = pathlib.Path(data_aggregator_mcp.__file__).parent
    calls, hidden = 0, []
    for path in sorted(src.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in ("request_json", "request_json_with_headers"):
                continue
            calls += 1
            if "expect" not in {k.arg for k in node.keywords}:
                hidden.append(f"{path.name}:{node.lineno}")
    assert calls >= 40
    assert hidden == []


# A body read outside `_http` on purpose. OpenML's 412 is an error status, not an
# answer: the body only says what it means ("no results" code 372, "unknown dataset" 111).
_PARSES_JSON_ITSELF = {"openml.py:_error_code"}


def _functions_calling_json(tree: ast.AST, path: str) -> list[str]:
    """``<file>:<innermost function>`` for each ``.json()`` call with no arguments."""
    found: list[str] = []

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "json"
            and not node.args
            and not node.keywords
        ):
            found.append(f"{path}:{owner}")
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return found


def test_only_the_http_layer_parses_a_json_body() -> None:
    """`expect` checks a body only if `request_json` parses it. fulltext, idconv and
    scholix took the response from `request_with_retry` and called `.json()` themselves,
    so EuropePMC's 200 error envelope read as "no open-access copy", an idconv body
    without records as "not in PMC", and a Scholix outage sank the resolve it enriches;
    the `expect` scan above never saw them, since it looks only at `request_json` calls.
    Positive control: the scan finds `_http`'s own parser."""
    src = pathlib.Path(data_aggregator_mcp.__file__).parent
    found = [
        hit
        for path in sorted(src.glob("*.py"))
        for hit in _functions_calling_json(ast.parse(path.read_text()), path.name)
    ]
    assert "_http.py:parse" in found
    assert sorted(set(found) - {"_http.py:parse"} - _PARSES_JSON_ITSELF) == []
