"""S1.6 elicitation — capability gating and the fail-soft ladder."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from typing import Any

import anyio
import httpx
import pytest
from mcp import types

from data_aggregator_mcp import elicitation


class _Session:
    """Minimal stand-in for an MCP ServerSession."""

    def __init__(self, capabilities: types.ClientCapabilities | None, result: Any = None) -> None:
        self.client_params = (
            None
            if capabilities is None
            else types.InitializeRequestParams(
                protocolVersion="2025-06-18",
                capabilities=capabilities,
                clientInfo=types.Implementation(name="t", version="0"),
            )
        )
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def elicit_form(self, message, requestedSchema, related_request_id=None):  # noqa: N803
        self.calls.append((message, requestedSchema))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _caps(*, form: bool = False, url: bool = False) -> types.ClientCapabilities:
    return types.ClientCapabilities(
        elicitation=types.ElicitationCapability(
            form=types.FormElicitationCapability() if form else None,
            url=types.UrlElicitationCapability() if url else None,
        )
    )


def _accept(**content: Any) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content=content)


# --- capability gating --------------------------------------------------------


def test_form_capability_is_detected() -> None:
    assert elicitation.supports_form_elicitation(_Session(_caps(form=True))) is True


def test_url_only_client_is_not_treated_as_form_capable() -> None:
    """The SDK's own ``check_client_capability`` stops at ``elicitation is None``
    (mcp 2.2.0 server/connection.py::check_capability, which says it mirrors the v1
    check verbatim) and would say yes here. The spec makes the two
    modes independent and requires only that a client support ONE of them, so sending
    a form request to a URL-only client is a protocol violation."""
    session = _Session(_caps(url=True))
    assert elicitation.supports_form_elicitation(session) is False


@pytest.mark.parametrize(
    ("declared", "form_capable"),
    [
        ({"elicitation": {}}, True),
        ({"elicitation": {"form": {}}}, True),
        ({"elicitation": {"form": {}, "url": {}}}, True),
        ({"elicitation": {"url": {}}}, False),
        ({}, False),
    ],
)
def test_bare_elicitation_capability_means_form_mode(declared, form_capable) -> None:
    """MCP 2025-11-25 and 2026-07-28, client/elicitation "Capabilities": "an empty
    capabilities object is equivalent to declaring support for ``form`` mode only".
    In 2025-06-18 the bare ``elicitation: {}`` was the only shape, so reading it as
    "no form" left every such client unprompted. Parsed from the wire JSON, as the
    server receives it; the url-only and undeclared rows are the negative controls."""
    caps = types.ClientCapabilities.model_validate(declared)
    assert elicitation.supports_form_elicitation(_Session(caps)) is form_capable


async def test_a_client_declaring_bare_elicitation_is_prompted(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    bare = types.ClientCapabilities.model_validate({"elicitation": {}})
    session = _Session(bare, _accept(organism="Saccharomyces cerevisiae"))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert len(session.calls) == 1
    assert out == {"organism": "Saccharomyces cerevisiae"}


def test_client_without_elicitation_is_not_capable() -> None:
    assert elicitation.supports_form_elicitation(_Session(types.ClientCapabilities())) is False


def test_uninitialized_session_is_not_capable() -> None:
    assert elicitation.supports_form_elicitation(_Session(None)) is False


# --- the fail-soft ladder: every rung yields "no corrections" -----------------


@pytest.mark.parametrize(
    ("session_factory", "label"),
    [
        (lambda: None, "no session at all (called outside an MCP request)"),
        (lambda: _Session(_caps(url=True)), "url-only client"),
        (lambda: _Session(types.ClientCapabilities()), "no elicitation capability"),
    ],
)
async def test_incapable_clients_never_prompt(monkeypatch, session_factory, label) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _never_resolves := _make_resolver(False))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session_factory(), {"organism": "yeast"})
    assert out == {}, label
    assert _never_resolves.calls == 0, f"{label}: must not even look up the term"


def _make_resolver(resolves: bool):
    async def _fake(client, field, value):
        _fake.calls += 1
        return resolves

    _fake.calls = 0
    return _fake


async def test_no_prompt_when_every_term_resolves(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(True))
    session = _Session(_caps(form=True), _accept(organism="Mus musculus"))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "mouse"})
    assert out == {}
    assert session.calls == []


async def test_no_prompt_when_no_ontology_param_was_supplied(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(_caps(form=True), _accept())
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(
            client, session, {"organism": None, "disease": "", "tissue": "  "}
        )
    assert out == {}
    assert session.calls == []


async def test_unresolved_term_is_corrected(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(_caps(form=True), _accept(organism="Saccharomyces cerevisiae"))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert out == {"organism": "Saccharomyces cerevisiae"}
    message, schema = session.calls[0]
    assert "yeast" in message and "NCBI Taxonomy" in message
    # the restricted schema subset forbids nesting
    assert schema["properties"]["organism"]["type"] == "string"
    assert all(p["type"] == "string" for p in schema["properties"].values())


async def test_one_form_covers_every_unresolved_field(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(
        _caps(form=True), _accept(organism="Quercus robur", tissue="", chemical="sucrose")
    )
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(
            client, session, {"organism": "oak", "tissue": "root", "chemical": "sugar"}
        )
    assert len(session.calls) == 1, "one prompt, not one per field"
    assert set(session.calls[0][1]["properties"]) == {"organism", "tissue", "chemical"}
    # tissue came back blank — a deliberate "search without it", not a correction
    assert out == {"organism": "Quercus robur", "chemical": "sucrose"}


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_declining_leaves_the_params_untouched(monkeypatch, action) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(_caps(form=True), types.ElicitResult(action=action))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert out == {}


async def test_a_broken_client_cannot_break_the_search(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(_caps(form=True), RuntimeError("transport gone"))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert out == {}


async def test_malformed_client_content_is_ignored(monkeypatch) -> None:
    """A client that answers with a non-string (or omits the key) must not have that
    value spliced into the search params."""
    monkeypatch.setattr(elicitation, "_resolves", _make_resolver(False))
    session = _Session(_caps(form=True), _accept(organism=42))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert out == {}


async def test_a_registry_outage_does_not_prompt(monkeypatch) -> None:
    """A lookup that RAISES is the router's to report in errors[]. Prompting the user to
    retype a perfectly good term because NCBI was briefly down would be misleading."""

    async def boom(client, name):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(elicitation.taxonomy, "resolve_taxon", boom)
    session = _Session(_caps(form=True), _accept(organism="Mus musculus"))
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, {"organism": "mouse"})
    assert out == {}
    assert session.calls == []


async def test_resolvers_bind_at_call_time_so_the_registry_can_be_patched(monkeypatch) -> None:
    """The three end-to-end tests in this file patch ``elicitation.taxonomy.resolve_taxon``.
    That only takes effect if ``_RESOLVERS`` looks the function up when called; a table
    that stored the function object at import silently kept calling the live NCBI
    resolver from required CI. Positive control: with the patch removed, the table
    reaches the module's current attribute."""
    calls: list[str] = []

    async def recorder(client, name):
        calls.append(name)
        return None

    monkeypatch.setattr(elicitation.taxonomy, "resolve_taxon", recorder)
    async with httpx.AsyncClient() as client:
        assert await elicitation._resolves(client, "organism", "yeast") is False
    assert calls == ["yeast"], "the patched resolver was never reached"

    monkeypatch.undo()
    resolver, _, _ = elicitation._RESOLVERS["organism"]
    assert resolver.__code__ is not elicitation.taxonomy.resolve_taxon.__code__
    import inspect

    assert "taxonomy.resolve_taxon" in inspect.getsource(resolver)


async def test_resolver_map_covers_exactly_the_search_ontology_params() -> None:
    """Guard against the map and the router's field list drifting apart."""
    from data_aggregator_mcp import router

    assert set(elicitation._RESOLVERS) == {f for f, _, _ in router._ONTOLOGY_FIELDS}


# --- real-execution boundary check -------------------------------------------
# The stubs above prove the module's logic. This drives the ACTUAL MCP protocol:
# a real ClientSession, a real elicitation/create round-trip, and the real server
# tool dispatch — the layer where a wrong capability check or a malformed schema
# would surface and unit tests could not see it.


@contextlib.asynccontextmanager
async def _connected_session(server, **client_kwargs):
    """A real ClientSession wired to ``server`` over in-memory streams.

    mcp 2.x removed ``create_connected_server_and_client_session``; this is the
    replacement its migration guide shows (``create_client_server_memory_streams``
    plus ``server.run`` in a task group). ``raise_exceptions=True`` so a handler
    bug fails the test instead of becoming a wire error."""
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(
            lambda: server.run(
                *server_streams, server.create_initialization_options(), raise_exceptions=True
            )
        )
        async with ClientSession(*client_streams, **client_kwargs) as session:
            await session.initialize()
            yield session
        tg.cancel_scope.cancel()


async def test_end_to_end_elicitation_over_a_real_mcp_session(monkeypatch) -> None:
    from data_aggregator_mcp import router
    from data_aggregator_mcp import server as server_mod

    seen: dict[str, Any] = {}
    prompts: list[str] = []

    async def fake_search_page(client, **kwargs):
        seen.update(kwargs)
        return router.SearchResult(query=kwargs.get("query") or "", total=0, count=0)

    async def fake_resolve_taxon(client, name):
        # "yeast" is genuinely absent from NCBI Taxonomy (probed live 2026-07-23)
        return None if name == "yeast" else object()

    monkeypatch.setattr(router, "search_page", fake_search_page)
    monkeypatch.setattr(elicitation.taxonomy, "resolve_taxon", fake_resolve_taxon)

    async def elicitation_callback(context, params):
        prompts.append(params.message)
        assert params.mode == "form"
        assert set(params.requested_schema["properties"]) == {"organism"}
        return types.ElicitResult(action="accept", content={"organism": "Saccharomyces cerevisiae"})

    async with _connected_session(
        server_mod.server, elicitation_callback=elicitation_callback
    ) as session:
        await session.call_tool("search", {"query": "fermentation", "organism": "yeast"})

    assert len(prompts) == 1 and "yeast" in prompts[0]
    assert seen["organism"] == "Saccharomyces cerevisiae", "the correction must reach the search"


async def test_end_to_end_a_client_without_elicitation_still_searches(monkeypatch) -> None:
    """The same call from a client that advertises no elicitation capability must run
    the search unchanged rather than hanging, erroring, or dropping the request."""
    from data_aggregator_mcp import router
    from data_aggregator_mcp import server as server_mod

    seen: dict[str, Any] = {}

    async def fake_search_page(client, **kwargs):
        seen.update(kwargs)
        return router.SearchResult(query=kwargs.get("query") or "", total=0, count=0)

    async def fake_resolve_taxon(client, name):
        return None

    monkeypatch.setattr(router, "search_page", fake_search_page)
    monkeypatch.setattr(elicitation.taxonomy, "resolve_taxon", fake_resolve_taxon)

    async with _connected_session(server_mod.server) as session:
        result = await session.call_tool("search", {"query": "fermentation", "organism": "yeast"})

    assert result.is_error is not True
    assert seen["organism"] == "yeast", "uncorrected param passes through untouched"


# --- live: the real server over stdio ------------------------------------------
# The in-memory tests above patch the registry. These spawn ``python -m
# data_aggregator_mcp`` and look the term up in the real NCBI Taxonomy, so they run
# only with DATA_AGGREGATOR_MCP_LIVE=1. The unresolvable term is made up rather than
# a common name ("yeast"), so the premise does not depend on what NCBI indexes; the
# replacement is the canonical name of a taxon NCBI has held for decades (taxid 4932).

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_NO_SUCH_TAXON = "zzqxv nonexistent organism"
_SEARCH_ARGS = {
    "query": "fermentation",
    "organism": _NO_SUCH_TAXON,
    "sources": ["zenodo"],
    "size": 1,
}


@live_only
async def test_live_accept_decline_and_cancel_over_stdio() -> None:
    """A real ClientSession with an elicitation callback, against the real stdio server.
    Accept applies the correction (the taxon is expanded, nothing is unresolved);
    decline and cancel run the search without the filter and report it unresolved."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    answers = {
        "accept": types.ElicitResult(
            action="accept", content={"organism": "Saccharomyces cerevisiae"}
        ),
        "decline": types.ElicitResult(action="decline"),
        "cancel": types.ElicitResult(action="cancel"),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "data_aggregator_mcp"])
    for action, answer in answers.items():
        asked: list[types.ElicitRequestParams] = []

        async def callback(context, request, answer=answer, asked=asked):
            asked.append(request)
            return answer

        with anyio.fail_after(120):
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write, elicitation_callback=callback) as session,
            ):
                await session.initialize()
                result = await session.call_tool("search", _SEARCH_ARGS)
        assert result.is_error is False, (action, result.content)
        out = result.structured_content
        assert len(asked) == 1, action
        assert asked[0].mode == "form"
        assert set(asked[0].requested_schema["properties"]) == {"organism"}
        if action == "accept":
            assert out["unresolved"] == []
            assert out["taxon_expansion"]["taxid"] == 4932
        else:
            assert out["taxon_expansion"] is None, action
            assert [(u["field"], u["input"]) for u in out["unresolved"]] == [
                ("organism", _NO_SUCH_TAXON)
            ], action


def _raw_stdio_search(capabilities: dict[str, Any]) -> tuple[list[dict], dict]:
    """Drive the stdio server with hand-written JSON-RPC, so the client can declare
    capabilities exactly as a 2025-06-18 client does (the SDK client always declares
    both modes). Answers any elicitation/create with an accept. Returns the
    elicitation requests seen and the search's structured content."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "data_aggregator_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    killer = threading.Timer(120, proc.kill)  # a hang ends as a closed pipe, not a stall
    killer.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None

        def send(message: dict) -> None:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            proc.stdin.flush()

        def receive() -> dict:
            while line := proc.stdout.readline():
                message = json.loads(line)
                if "id" in message:
                    return message
            raise AssertionError("the server closed stdout (or the 120 s guard fired)")

        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": capabilities,
                    "clientInfo": {"name": "raw", "version": "0"},
                },
            }
        )
        assert receive()["result"]["protocolVersion"] == "2025-06-18"
        send({"method": "notifications/initialized"})
        send(
            {
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": _SEARCH_ARGS},
            }
        )
        asked: list[dict] = []
        while (message := receive()).get("method") == "elicitation/create":
            asked.append(message["params"])
            send(
                {
                    "id": message["id"],
                    "result": {
                        "action": "accept",
                        "content": {"organism": "Saccharomyces cerevisiae"},
                    },
                }
            )
        assert message["id"] == 2
        return asked, message["result"]["structuredContent"]
    finally:
        killer.cancel()
        proc.kill()
        proc.wait()


@live_only
def test_live_a_bare_elicitation_capability_is_prompted_over_stdio() -> None:
    """A 2025-06-18 client declares ``elicitation: {}``; it is form mode and is asked.
    Negative control in the same test: a url-only client is never sent a form."""
    asked, out = _raw_stdio_search({"elicitation": {}})
    assert [set(a["requestedSchema"]["properties"]) for a in asked] == [{"organism"}]
    assert out["unresolved"] == []
    assert out["taxon_expansion"]["taxid"] == 4932

    asked, out = _raw_stdio_search({"elicitation": {"url": {}}})
    assert asked == []
    assert [u["field"] for u in out["unresolved"]] == ["organism"]
