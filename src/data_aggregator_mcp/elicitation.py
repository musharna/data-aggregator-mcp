"""Capability-gated elicitation for ontology params that don't resolve (S1.6).

``search`` accepts five ontology-typed params (organism/disease/tissue/chemical/
assay). Each is looked up in a registry, and a lookup that finds nothing leaves the
query un-expanded — the caller's filter is simply dropped. ``SearchResult.unresolved``
reports that after the fact; this module lets a capable client fix it BEFORE the
search runs, by asking the user for a replacement term.

**What this deliberately does NOT do: offer a "did you mean …" candidate list.**
A live probe (2026-07-23) of the relaxed lookups showed the suggestions are usable for
EDAM (``alignment`` → *Nucleic acid sequence alignment*) but noise elsewhere
(``sugar`` → *sugar-phosphodiester opine*; ``body`` → *mucosa of body of stomach*;
``root`` → *ventral root of spinal cord*, when a plant root was almost certainly
meant — that term lives in PO, not UBERON), and empty for taxonomy, which indexes
none of ``yeast``/``oak``/``cedar``/``bass``. Quality varies by ontology AND by term
with no reliable programmatic discriminator, so presenting a generated list as a
disambiguation would be dressing up noise as an answer. We ask for a free-text
correction instead, which needs no such guarantee.

The same probe found genuine multi-candidate ambiguity in only 2 of 20 terms, and the
existing top-hit heuristic picked correctly in both — so there is deliberately no
"which one did you mean" prompt either. That would interrupt the user to re-ask a
question the server already answers right.

Fail-soft by construction: no session, no client, no elicitation capability, a client
that only does URL mode, a transport error, or a user who declines all land on the
same outcome — no replacements, and the search proceeds exactly as it does today
(with the ``unresolved`` echo intact). This module NEVER raises into the search path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp import types

from data_aggregator_mcp import anatomy, assay, chemistry, mesh, taxonomy

logger = logging.getLogger(__name__)

# field -> (resolver, registry label, the hint shown in the form)
_RESOLVERS: dict[str, tuple[Callable[[httpx.AsyncClient, str], Awaitable[Any]], str, str]] = {
    "organism": (
        taxonomy.resolve_taxon,
        "NCBI Taxonomy",
        "a scientific name, e.g. 'Saccharomyces cerevisiae' for yeast",
    ),
    "disease": (
        mesh.resolve_mesh,
        "MeSH",
        "a MeSH descriptor, e.g. 'Breast Neoplasms'",
    ),
    "tissue": (
        anatomy.resolve_uberon,
        "UBERON",
        "an UBERON term, e.g. 'liver' or 'cerebral cortex'",
    ),
    "chemical": (
        chemistry.resolve_chebi,
        "ChEBI",
        "a ChEBI compound name, e.g. 'caffeine'",
    ),
    "assay": (
        assay.resolve_edam,
        "EDAM",
        "an EDAM method or topic, e.g. 'ChIP-seq'",
    ),
}


def supports_form_elicitation(session: Any) -> bool:
    """True only when the client advertised **form** mode.

    Deliberately not ``session.check_client_capability`` — that helper stops at
    ``client_caps.elicitation is None`` (mcp 1.28.1 ``server/session.py:153``) and never
    looks at the sub-capability, so it returns True for a URL-only client. The spec
    treats the two modes as independent and requires only that a client support at
    least one (``types.py:319``), so a URL-only client is a real shape — and sending it
    a form request would be a protocol violation we'd have talked ourselves into.
    """
    params = getattr(session, "client_params", None)
    caps = getattr(params, "capabilities", None)
    elicitation = getattr(caps, "elicitation", None)
    return getattr(elicitation, "form", None) is not None


async def _resolves(client: httpx.AsyncClient, field: str, value: str) -> bool:
    """Whether ``value`` matches a term in ``field``'s registry.

    A lookup that RAISES counts as resolved-enough: the failure is the router's to
    surface in ``errors`` (fail-loud), and prompting the user to retype a perfectly
    good term because the registry was briefly down would be actively misleading.
    """
    resolver, _, _ = _RESOLVERS[field]
    try:
        return await resolver(client, value) is not None
    except Exception as exc:  # noqa: BLE001 - the router reports this; we only gate the prompt
        logger.debug("elicitation pre-flight: %s lookup for %r failed: %r", field, value, exc)
        return True


def _build_schema(pending: list[tuple[str, str, str]]) -> types.ElicitRequestedSchema:
    """One form, one top-level string property per unresolved field (the schema subset
    forbids nesting). Every field is optional — leaving one blank means "search without
    that expansion", which must stay a first-class choice, not a dead end."""
    return {
        "type": "object",
        "properties": {
            field: {
                "type": "string",
                "title": f"{field} (not found in {ontology})",
                "description": f"Enter {hint} — or leave blank to search without it.",
            }
            for field, ontology, hint in pending
        },
    }


def _message(pending: list[tuple[str, str, str]]) -> str:
    parts = [f"{field}={value!r} was not found in {ontology}" for field, ontology, value in pending]
    joined = "; ".join(parts)
    return (
        f"Could not resolve {joined}. "
        "Those filters will be dropped and the search will run without them. "
        "Enter a replacement for any you want applied, or leave blank to proceed."
    )


async def correct_unresolved(
    client: httpx.AsyncClient,
    session: Any,
    params: dict[str, str | None],
    *,
    related_request_id: Any = None,
) -> dict[str, str]:
    """Pre-flight the ontology params and ask the user to fix any that match nothing.

    Returns ``{field: replacement}`` for fields the user actually corrected — empty
    when there is nothing to ask, nobody to ask, or the user declined. The caller
    merges it into the search params.

    Runs BEFORE the search rather than retrying after one: the resolvers are
    in-process cached (``TTLCache``), so the lookup done here is the same lookup the
    router would do moments later and costs no extra upstream request — whereas
    correcting after the fact would mean a second full fan-out across every adapter.
    """
    if session is None or not supports_form_elicitation(session):
        return {}

    pending: list[tuple[str, str, str]] = []
    for field, (_, ontology, hint) in _RESOLVERS.items():
        value = params.get(field)
        if not value or not value.strip():
            continue
        if await _resolves(client, field, value):
            continue
        pending.append((field, ontology, hint))
    if not pending:
        return {}

    prompt_pending = [(f, o, params[f] or "") for f, o, _ in pending]
    try:
        result = await session.elicit_form(
            _message(prompt_pending),
            _build_schema(pending),
            related_request_id,
        )
    except Exception as exc:  # noqa: BLE001 - an unreachable/refusing client must not break search
        logger.warning("elicitation failed, proceeding un-corrected: %r", exc)
        return {}

    if getattr(result, "action", None) != "accept":
        return {}
    content = getattr(result, "content", None) or {}
    corrections: dict[str, str] = {}
    for field, _, _ in pending:
        replacement = content.get(field)
        # Blank is a deliberate answer ("proceed without it"), not a correction. Anything
        # non-string is a malformed client response — ignore rather than trust it.
        if isinstance(replacement, str) and replacement.strip():
            corrections[field] = replacement.strip()
    return corrections
