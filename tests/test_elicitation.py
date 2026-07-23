"""S1.6 elicitation — capability gating and the fail-soft ladder."""

from __future__ import annotations

from typing import Any

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
    (mcp 1.28.1 server/session.py:153) and would say yes here. The spec makes the two
    modes independent and requires only that a client support ONE of them, so sending
    a form request to a URL-only client is a protocol violation."""
    session = _Session(_caps(url=True))
    assert elicitation.supports_form_elicitation(session) is False


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


async def test_resolver_map_covers_exactly_the_search_ontology_params() -> None:
    """Guard against the map and the router's field list drifting apart."""
    from data_aggregator_mcp import router

    assert set(elicitation._RESOLVERS) == {f for f, _, _ in router._ONTOLOGY_FIELDS}


# --- real-execution boundary check -------------------------------------------
# The stubs above prove the module's logic. This drives the ACTUAL MCP protocol:
# a real ClientSession, a real elicitation/create round-trip, and the real server
# tool dispatch — the layer where a wrong capability check or a malformed schema
# would surface and unit tests could not see it.


async def test_end_to_end_elicitation_over_a_real_mcp_session(monkeypatch) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

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
        assert set(params.requestedSchema["properties"]) == {"organism"}
        return types.ElicitResult(action="accept", content={"organism": "Saccharomyces cerevisiae"})

    async with create_connected_server_and_client_session(
        server_mod.server, elicitation_callback=elicitation_callback, raise_exceptions=True
    ) as session:
        await session.call_tool("search", {"query": "fermentation", "organism": "yeast"})

    assert len(prompts) == 1 and "yeast" in prompts[0]
    assert seen["organism"] == "Saccharomyces cerevisiae", "the correction must reach the search"


async def test_end_to_end_a_client_without_elicitation_still_searches(monkeypatch) -> None:
    """The same call from a client that advertises no elicitation capability must run
    the search unchanged rather than hanging, erroring, or dropping the request."""
    from mcp.shared.memory import create_connected_server_and_client_session

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

    async with create_connected_server_and_client_session(
        server_mod.server, raise_exceptions=True
    ) as session:
        result = await session.call_tool("search", {"query": "fermentation", "organism": "yeast"})

    assert result.isError is not True
    assert seen["organism"] == "yeast", "uncorrected param passes through untouched"
