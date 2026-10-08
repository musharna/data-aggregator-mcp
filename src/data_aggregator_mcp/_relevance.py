"""Match tiers: the order of the default ``rank="relevance"`` page.

The streams of a search are merged round-robin, so each source's first hit took a top
slot however little of the search it named: a source that cannot apply the ontology
facets (keyword-only) or that matches loosely upstream put a crustacean or a cobra
above the hits about the organism asked for. Each fetched hit is now scored by what it
names, checked locally in its title, description, subjects and organisms:

1. how many facets (organism, disease, tissue, chemical, assay) it names, any of each
   facet's expanded names counting;
2. whether its title names the query as written (its words in order);
3. whether it names the query as written anywhere;
4. how many words of the query it names.

Tiers 2 and 3 exist because a name is its words in order: every one of the first 61
hits for "snow leopard" named both words and tied, so Antarctic station logs ("snow"
in one line, "leopard seals" in another) and a museum's whole mammal collection took
the slots of the snow leopard studies (2026-10-05).

The page is stably sorted by that score, so hits that score the same keep the
round-robin order, and nothing is dropped. Matching is whole-word after case folding,
with punctuation and hyphens read as spaces ("single-cell" names "single cell"); a
query word also matches its plural with "s".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from data_aggregator_mcp.models import DataResource

if TYPE_CHECKING:  # _ontology imports taxonomy, which imports this module
    from data_aggregator_mcp._ontology import FacetGroup

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


def query_phrase(query: str) -> str:
    """The query as written: its words in order, normalized, without the boolean
    operators and quotes (stop words kept: "seals of antarctica" is what a title says)."""
    return " ".join(
        w
        for phrase, word in _PHRASE_OR_WORD.findall(query)
        if word not in _OPERATORS
        for w in [_normalize(phrase or word).strip()]
        if w
    )


# A parenthesis, a double-quoted phrase, or a run of anything else; no nested quantifier.
_QUERY_TOKEN = re.compile(r'[()]|"[^"]*"|[^\s"()]+')


def with_plurals(query: str) -> str:
    """``query`` with each plain word sent as ``(word OR words)``, for an upstream that
    matches words exactly, so it finds what :func:`_names` counts as naming the word.
    DataCite found "Spatial ecology of snow leopards" for "snow leopards" but not for
    "snow leopard" (probed 2026-10-05). Only words of three or more letters change:
    a quoted phrase, an operator, a stop word, a word ending in "s", and anything holding
    a field, wildcard or other syntax are sent as written."""

    def plural(m: re.Match[str]) -> str:
        word = m[0]
        return f"({word} OR {word}s)" if _pluralisable(word) else word

    return _QUERY_TOKEN.sub(plural, query)


def _pluralisable(word: str) -> bool:
    return (
        word.isalpha()
        and len(word) >= 3
        and word not in _OPERATORS
        and word.casefold() not in _STOP_WORDS
        and not word.casefold().endswith("s")
    )


# Words of letters, digits, hyphens and apostrophes, separated by spaces: nothing an
# upstream query language reads as syntax.
_PLAIN_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*(?: +[A-Za-z0-9][A-Za-z0-9'-]*)*")


def title_clause(query: str, field: str) -> str | None:
    """A clause matching records whose ``field`` holds ``query`` as written, its last
    word also as a plural: ``title:("snow leopard" OR "snow leopards")``. None unless
    ``query`` is plain words; an operator, quote, field or wildcard means the caller
    already wrote the query they want."""
    words = query.split()
    if not _PLAIN_WORDS.fullmatch(" ".join(words)) or any(w in _OPERATORS for w in words):
        return None
    phrase = " ".join(words)
    if not _pluralisable(words[-1]):
        return f'{field}:"{phrase}"'
    return f'{field}:("{phrase}" OR "{phrase}s")'


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
        self.phrase = query_phrase(query)
        self.facets = [[t for t in (_normalize(n).strip() for n in g.terms) if t] for g in groups]

    def score(self, record: DataResource) -> tuple[int, int, int, int]:
        """(facets named, phrase in title, phrase anywhere, query terms named): compare as
        a tuple, higher first. An empty query names no phrase."""
        text = _text(record)
        facets = sum(any(_names(text, n) for n in names) for names in self.facets)
        in_title = bool(self.phrase) and _names(_normalize(record.title), self.phrase)
        anywhere = bool(self.phrase) and _names(text, self.phrase)
        return facets, int(in_title), int(anywhere), sum(_names(text, t) for t in self.terms)

    def names_every_term(self, record: DataResource) -> bool:
        """Whether ``record`` names every term of the query; never for an empty query."""
        return bool(self.terms) and self.score(record)[3] == len(self.terms)

    def unshown(self, record: DataResource) -> list[str]:
        """The query terms ``record``'s text does not name, in query order."""
        text = _text(record)
        return [t for t in self.terms if not _names(text, t)]
