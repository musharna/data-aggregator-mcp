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
    "keyword_core (string: the query rewritten as a focused keyword search. Remove ONLY "
    "conversational fluff such as 'I am looking for', 'where can I find', 'datasets about', "
    "'show me'. KEEP every scientific and entity term — organism, tissue, assay, disease, "
    "chemical names — so they still match by text. Example: "
    "'I'm looking for single-cell RNA-seq of human liver tissue' -> "
    "'single-cell RNA-seq human liver'), "
    "organism, disease, tissue, chemical, assay (each: a single canonical entity NAME "
    "or null - do NOT invent; null if not clearly present. These are ADVISORY LABELS for "
    "transparency; they do NOT remove the term from keyword_core), "
    "kind (one of dataset|sequencing_run|study|publication|software or null), "
    "year_min, year_max (integers or null), "
    "confidence (number between 0 and 1 or null: your self-assessed, UNCALIBRATED "
    "confidence that this structured rewrite matches the user's intent — advisory only). "
    "Do not add keys. Do not explain."
)

_EXPAND_SYSTEM_PROMPT = (
    "Generate up to {n} ALTERNATIVE search queries that capture DIFFERENT facets, "
    "synonyms, and framings of the user's intent — genuinely diverse reformulations, "
    'NOT paraphrases. Return STRICT JSON {{"variants": [string, ...]}}. '
    "Omit the original query. Do not explain."
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
    confidence: float | None = None  # advisory, UNCALIBRATED LLM self-confidence in [0, 1]


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


def _clean_float(value: object) -> float | None:
    """Coerce to a float clamped to [0, 1], else None. Never raises. Used for the
    advisory, uncalibrated confidence axis — bool and non-numeric strings → None."""
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return max(0.0, min(1.0, f))


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
        confidence=_clean_float(data.get("confidence")),
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


async def expand(client: httpx.AsyncClient, query: str, *, n: int) -> list[str] | None:
    """Generate up to ``n - 1`` deliberately-diverse ALTERNATIVE reformulations of ``query``
    via the configured LLM endpoint (A2.P2 multi-query recall expansion).

    The prompt demands genuinely diverse reformulations (different facets/synonyms/framings,
    not paraphrases); we parse the LLM's ``variants`` list defensively (non-empty strings
    only). Returns None when no endpoint is configured, on any LLM/parse failure, or when the
    expansion yields nothing usable. The CALLER prepends the original query as variant 0,
    case-insensitively dedups, and caps to ``MAX_QUERY_VARIANTS``. NEVER raises into the
    search path (same fail-soft discipline as ``rewrite``)."""
    if llm._config() is None:
        return None
    system = _EXPAND_SYSTEM_PROMPT.format(n=max(n - 1, 1))
    data = await llm.complete_json(client, system=system, user=query)
    if data is None:
        return None
    raw = data.get("variants")
    if not isinstance(raw, list):
        return None
    variants = [s for s in (_clean_str(v) for v in raw) if s is not None]
    return variants or None
