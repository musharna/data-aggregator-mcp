"""Match tiers: the order of the default ``rank="relevance"`` page.

The streams of a search are merged round-robin, so each source's first hit took a top
slot however little of the search it named: a source that cannot apply the ontology
facets (keyword-only) or that matches loosely upstream put a crustacean or a cobra
above the hits about the organism asked for. Each fetched hit is now scored by what it
names, checked locally in its title, description, subjects and organisms:

1. how many facets (organism, disease, tissue, chemical, assay) it names, any of each
   facet's expanded names counting;
2. how many words of the query it names.

The page is stably sorted by that score, so hits that score the same keep the
round-robin order, and nothing is dropped. Matching is whole-word after case folding,
with punctuation and hyphens read as spaces ("single-cell" names "single cell"); a
query word also matches its plural with "s".
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from data_aggregator_mcp._ontology import FacetGroup
from data_aggregator_mcp.models import DataResource

_NON_WORD = re.compile(r"[\W_]+")
# A double-quoted phrase or a run of non-space characters; neither nests a quantifier.
_PHRASE_OR_WORD = re.compile(r'"([^"]*)"|(\S+)')
_OPERATORS = frozenset({"AND", "OR", "NOT"})
# Words nearly every record contains: matching them says nothing about the record.
_STOP_WORDS = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the",
     "to", "with"}
)  # fmt: skip


def _normalize(text: str) -> str:
    """Case-folded words joined by single spaces, padded so ``f" {term} "`` tests a
    whole-word match."""
    words = _NON_WORD.sub(" ", text.casefold()).split()
    return f" {' '.join(words)} "


def query_terms(query: str) -> list[str]:
    """The terms of a query a hit can name: each quoted phrase whole, each other word
    alone, minus the boolean operators and stop words, normalized and de-duplicated."""
    terms: list[str] = []
    for phrase, word in _PHRASE_OR_WORD.findall(query):
        if word in _OPERATORS:
            continue
        term = _normalize(phrase or word).strip()
        if term and term not in _STOP_WORDS:
            terms.append(term)
    return list(dict.fromkeys(terms))


def _text(record: DataResource) -> str:
    return _normalize(
        " ".join(
            [
                record.title,
                record.description or "",
                *record.subjects,
                *record.organism,
                *(t.name for t in record.taxa),
            ]
        )
    )


def _names(text: str, term: str) -> bool:
    return f" {term} " in text or f" {term}s " in text


class MatchTiers:
    """Scores hits against one search: its query terms and its facet groups."""

    def __init__(self, query: str, groups: Sequence[FacetGroup]) -> None:
        self.terms = query_terms(query)
        self.facets = [[t for t in (_normalize(n).strip() for n in g.terms) if t] for g in groups]

    def score(self, record: DataResource) -> tuple[int, int]:
        """(facets named, query terms named): compare as a tuple, higher first."""
        text = _text(record)
        facets = sum(any(_names(text, n) for n in names) for names in self.facets)
        return facets, sum(_names(text, t) for t in self.terms)
