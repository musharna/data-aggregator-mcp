"""What the elicitation pre-flight sends and how it reads the answer, pinned exactly.

The client's answer is untrusted input. MCP 2025-11-25 and 2026-07-28 (client/elicitation,
"Form Mode Security"): "Servers SHOULD validate received data matches the requested
schema". The schema asks for one optional string per unresolved field, so only a
non-blank string under a field that was asked for is taken; everything else is dropped.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import types

from data_aggregator_mcp import anatomy, assay, chemistry, elicitation, mesh, taxonomy
from tests.test_elicitation import _accept, _caps

_FORM = _caps(form=True)


class _Recorder:
    """A form-capable session that records each elicit_form call whole."""

    def __init__(self, result: Any) -> None:
        self.client_params = SimpleNamespace(capabilities=_FORM)
        self._result = result
        self.calls: list[tuple[str, dict, Any]] = []

    async def elicit_form(self, message, requested_schema, related_request_id=None):
        self.calls.append((message, requested_schema, related_request_id))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _unresolved(*names: str):
    """A ``_resolves`` stand-in: the named fields match nothing; it records each lookup."""
    seen: list[tuple[Any, str, str]] = []

    async def fake(client, field, value):
        seen.append((client, field, value))
        return field not in names

    return fake, seen


# --- capability --------------------------------------------------------------


def test_a_session_object_without_client_params_is_not_capable() -> None:
    """``session`` is whatever the SDK's request context holds; one without
    ``client_params`` must read as "cannot ask", not raise into the search path."""
    assert elicitation.supports_form_elicitation(object()) is False
    assert elicitation.supports_form_elicitation(_Recorder(None)) is True


# --- the registry lookup --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "module", "name"),
    [
        ("organism", taxonomy, "resolve_taxon"),
        ("disease", mesh, "resolve_mesh"),
        ("tissue", anatomy, "resolve_uberon"),
        ("chemical", chemistry, "resolve_chebi"),
        ("assay", assay, "resolve_edam"),
    ],
)
async def test_each_field_is_looked_up_in_its_registry_with_the_shared_client(
    monkeypatch, field, module, name
) -> None:
    calls: list[tuple[Any, str]] = []
    answer: list[Any] = [None]

    async def registry(client, value):
        calls.append((client, value))
        return answer[0]

    monkeypatch.setattr(module, name, registry)
    async with httpx.AsyncClient() as client:
        assert await elicitation._resolves(client, field, "term") is False
        answer[0] = object()
        assert await elicitation._resolves(client, field, "term") is True
    assert calls == [(client, "term"), (client, "term")]


async def test_a_failed_lookup_counts_as_resolved_and_is_logged(monkeypatch, caplog) -> None:
    async def boom(client, value):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(taxonomy, "resolve_taxon", boom)
    caplog.set_level(logging.DEBUG, logger=elicitation.logger.name)
    async with httpx.AsyncClient() as client:
        assert await elicitation._resolves(client, "organism", "mouse") is True
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.DEBUG,
            "elicitation pre-flight: organism lookup for 'mouse' failed: ConnectError('nope')",
        )
    ]


# --- what is asked ------------------------------------------------------------


async def test_the_form_sent_is_exactly_this(monkeypatch) -> None:
    """Fields in registry order; a resolved field is skipped without ending the scan, a
    blank one is not looked up; the request id ties the prompt to the tool call."""
    fake, seen = _unresolved("disease", "chemical")
    monkeypatch.setattr(elicitation, "_resolves", fake)
    session = _Recorder(types.ElicitResult(action="decline"))
    params = {
        "organism": "Homo sapiens",
        "disease": "brest cancer",
        "tissue": "  ",
        "chemical": "sugar",
        "assay": None,
    }
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, params, related_request_id=7)
    assert out == {}
    assert seen == [
        (client, "organism", "Homo sapiens"),
        (client, "disease", "brest cancer"),
        (client, "chemical", "sugar"),
    ]
    assert session.calls == [
        (
            "Could not resolve disease='brest cancer' was not found in MeSH; "
            "chemical='sugar' was not found in ChEBI. "
            "Those filters will be dropped and the search will run without them. "
            "Enter a replacement for any you want applied, or leave blank to proceed.",
            {
                "type": "object",
                "properties": {
                    "disease": {
                        "type": "string",
                        "title": "disease (not found in MeSH)",
                        "description": "Enter a MeSH descriptor, e.g. 'Breast Neoplasms'"
                        " — or leave blank to search without it.",
                    },
                    "chemical": {
                        "type": "string",
                        "title": "chemical (not found in ChEBI)",
                        "description": "Enter a ChEBI compound name, e.g. 'caffeine'"
                        " — or leave blank to search without it.",
                    },
                },
            },
            7,
        )
    ]


async def test_without_a_request_id_none_is_sent(monkeypatch) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _unresolved("organism")[0])
    session = _Recorder(types.ElicitResult(action="cancel"))
    async with httpx.AsyncClient() as client:
        await elicitation.correct_unresolved(client, session, {"organism": "yeast"})
    assert [rid for _, _, rid in session.calls] == [None]


async def test_a_failed_prompt_is_logged_and_the_search_proceeds(monkeypatch, caplog) -> None:
    monkeypatch.setattr(elicitation, "_resolves", _unresolved("organism")[0])
    caplog.set_level(logging.WARNING, logger=elicitation.logger.name)
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(
            client, _Recorder(RuntimeError("transport gone")), {"organism": "yeast"}
        )
    assert out == {}
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            "elicitation failed, proceeding un-corrected: RuntimeError('transport gone')",
        )
    ]


# --- how the answer is read ---------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(action="accept"),
        SimpleNamespace(action="accept", content=None),
        types.ElicitResult(action="decline", content={"organism": "Mus musculus"}),
        types.ElicitResult(action="cancel", content={"organism": "Mus musculus"}),
    ],
    ids=["none", "no-action", "accept-no-content", "accept-null-content", "decline", "cancel"],
)
async def test_an_answer_without_accepted_content_corrects_nothing(monkeypatch, result) -> None:
    """Only ``accept`` carries data (spec "Response Actions"); content sent alongside a
    decline or cancel is not taken, and a session answering with no result object at
    all must not raise into the search. Positive control: the same call with a
    well-formed accept yields the correction."""
    monkeypatch.setattr(elicitation, "_resolves", _unresolved("organism")[0])
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, _Recorder(result), {"organism": "mouse"})
        ok = await elicitation.correct_unresolved(
            client, _Recorder(_accept(organism="Mus musculus")), {"organism": "mouse"}
        )
    assert out == {}
    assert ok == {"organism": "Mus musculus"}


async def test_only_non_blank_strings_for_asked_fields_are_taken(monkeypatch) -> None:
    """Out-of-schema answers are dropped: a field that was not asked for (``disease``
    resolved, so it is not in the form), a non-string, a list, a null. The one answer
    that matches the schema is taken, trimmed."""
    monkeypatch.setattr(
        elicitation, "_resolves", _unresolved("organism", "tissue", "chemical", "assay")[0]
    )
    answer = _accept(
        organism="  Mus musculus \n",
        disease="Neoplasms",
        tissue=42,
        chemical=["caffeine"],
        assay=None,
        extra="injected",
    )
    params = {
        "organism": "mouse",
        "disease": "cancer",
        "tissue": "lver",
        "chemical": "cafeine",
        "assay": "chipseq",
    }
    session = _Recorder(answer)
    async with httpx.AsyncClient() as client:
        out = await elicitation.correct_unresolved(client, session, params)
    assert set(session.calls[0][1]["properties"]) == {"organism", "tissue", "chemical", "assay"}
    assert out == {"organism": "Mus musculus"}
