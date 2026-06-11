"""LLM NL→structured-query rewriter (A2.P1).

Turns a free-text research-data query into a keyword core + structured params
(organism/disease/tissue/chemical/assay + kind/year). The LLM only PROPOSES; the
router's shipped ``_expand_*`` ontology resolvers (NCBI/MeSH/UBERON/ChEBI/EDAM) and
``_VALID_KINDS`` gate VALIDATE — a hallucinated entity that doesn't resolve simply
yields no expansion. Opt-in + fail-soft: when no LLM endpoint is configured (or any
error occurs) ``rewrite`` returns None and the caller keeps the raw query. NEVER raises.

This module performs NO ontology calls itself — validation happens downstream in the
router. Its only side effect is the single LLM call inside ``llm.complete_json``.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from data_aggregator_mcp import llm

# Mirror router._VALID_KINDS, kept local to avoid an import cycle (router imports
# this module). The rewriter drops any kind not in this set before it reaches the
# router, so an invalid kind is never passed downstream.
_VALID_KINDS = {"dataset", "sequencing_run", "study", "publication", "software"}

_SYSTEM_PROMPT = (
    "You convert a free-text research-data search query into a structured search. "
    "Return STRICT JSON with EXACTLY these keys: "
    "keyword_core (string: the core search terms with natural-language fluff removed), "
    "organism, disease, tissue, chemical, assay (each: a single canonical entity NAME "
    "or null - do NOT invent; null if not clearly present), "
    "kind (one of dataset|sequencing_run|study|publication|software or null), "
    "year_min, year_max (integers or null). "
    "Do not add keys. Do not explain."
)


@dataclass
class ParsedRewrite:
    """Internal: the validated structured interpretation of a query (the LLM proposes;
    the router's ontology resolvers dispose). All fields optional."""

    keyword_core: str | None = None
    organism: str | None = None
    disease: str | None = None
    tissue: str | None = None
    chemical: str | None = None
    assay: str | None = None
    kind: str | None = None
    year_min: int | None = None
    year_max: int | None = None


def _clean_str(value: object) -> str | None:
    """Coerce to a non-empty stripped string, else None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean_int(value: object) -> int | None:
    """Coerce to int (accepting a numeric str/float), else None. Never raises."""
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


async def rewrite(client: httpx.AsyncClient, query: str) -> ParsedRewrite | None:
    """Rewrite ``query`` into a ``ParsedRewrite`` via the configured LLM endpoint.

    Returns None when no endpoint is configured, on any LLM/parse failure, or when the
    rewrite yields nothing usable (all fields None). NEVER raises into the search path."""
    if llm._config() is None:
        return None
    data = await llm.complete_json(client, system=_SYSTEM_PROMPT, user=query)
    if data is None:
        return None

    parsed = ParsedRewrite(
        keyword_core=_clean_str(data.get("keyword_core")),
        organism=_clean_str(data.get("organism")),
        disease=_clean_str(data.get("disease")),
        tissue=_clean_str(data.get("tissue")),
        chemical=_clean_str(data.get("chemical")),
        assay=_clean_str(data.get("assay")),
        kind=_clean_str(data.get("kind")),
        year_min=_clean_int(data.get("year_min")),
        year_max=_clean_int(data.get("year_max")),
    )
    # Drop an invalid kind rather than pass it downstream (the router would reject it).
    if parsed.kind not in _VALID_KINDS:
        parsed.kind = None

    # Nothing usable → no-op rewrite; nothing to echo.
    if all(
        v is None
        for v in (
            parsed.keyword_core,
            parsed.organism,
            parsed.disease,
            parsed.tissue,
            parsed.chemical,
            parsed.assay,
            parsed.kind,
            parsed.year_min,
            parsed.year_max,
        )
    ):
        return None
    return parsed
