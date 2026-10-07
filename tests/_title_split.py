"""What a plain-words search sends Zenodo and DataCite: three parts that split the query
(test_title_tier.py, test_deposit_tier.py): the data deposits naming it in a title, the
other records naming it in a title, then the rest."""

from __future__ import annotations

import httpx

from data_aggregator_mcp import datacite, zenodo
from data_aggregator_mcp._relevance import title_clause, with_plurals

_HALF = {
    "zenodo": ("https://zenodo.org/api/records", "q", "title", {}, zenodo.DEPOSIT_CLAUSE),
    "datacite": (
        "https://api.datacite.org/dois",
        "query",
        "titles.title",
        {"sort": "relevance"},
        datacite.DEPOSIT_CLAUSE,
    ),
}
_EMPTY = {
    "zenodo": {"hits": {"total": 0, "hits": []}},
    "datacite": {"data": [], "meta": {"total": 0}},
}


def half_urls(source: str, query: str, size: int = 10) -> list[str]:
    """The deposits' URL, the other title matches', then the rest's, as the adapter
    sends them for ``query``."""
    base, param, field, extra, deposits = _HALF[source]
    words = with_plurals(query)
    clause = title_clause(query, field)
    size_key = "size" if source == "zenodo" else "page[size]"
    return [
        str(httpx.URL(base, params={param: f"({words}) AND {how}", **extra, size_key: str(size)}))
        for how in (f"{clause} AND {deposits}", f"{clause} AND NOT {deposits}", f"NOT {clause}")
    ]


def mock_halves(
    httpx_mock, source: str, query: str, *, size: int = 10, json=None, rest=None, deposits=None
) -> None:
    """``json`` answers the title matches that are not deposits, ``deposits`` the
    deposits and ``rest`` the records without a title match; any left out is empty."""
    deposits_url, title_url, rest_url = half_urls(source, query, size)
    for url, body in ((deposits_url, deposits), (title_url, json), (rest_url, rest)):
        httpx_mock.add_response(url=url, json=body if body is not None else _EMPTY[source])


def title_half(query: str) -> bool:
    """Whether ``query`` is one of the title parts of a split search."""
    return " AND title:" in query or " AND titles.title:" in query


def whole(sent: list[str]) -> str:
    """The query a source was sent. A plain-words search sends a source with a title
    field three parts that split it: ``(q) AND <title> AND <deposits>``, ``(q) AND
    <title> AND NOT <deposits>``, then ``(q) AND NOT <title>``; all three are checked to
    wrap the same ``q``. A kind filter sent upstream leaves the title matches whole: two
    parts, ``(q) AND <title>`` then the rest. (Neither clause holds " AND ": a query with
    an operator is never split.)"""
    if len(sent) == 1:
        return sent[0]
    if len(sent) == 2:
        title, rest = sent
        cut = title.rindex(" AND ")
        q, clause = title[1 : cut - 1], title[cut + 5 :]
        assert clause.startswith(("title:", "titles.title:")), clause
        assert title == f"({q}) AND {clause}" and rest == f"({q}) AND NOT {clause}"
        return q
    (rest,) = [s for s in sent if " AND NOT title" in s]
    (title,) = [s for s in sent if s != rest and " AND NOT " in s]
    (deposits,) = [s for s in sent if s not in (rest, title)]
    cut = rest.rindex(" AND NOT ")
    q, clause = rest[1 : cut - 1], rest[cut + 9 :]
    assert clause.startswith(("title:", "titles.title:")), clause
    kind = deposits[len(f"({q}) AND {clause} AND ") :]
    assert deposits == f"({q}) AND {clause} AND {kind}", deposits
    assert title == f"({q}) AND {clause} AND NOT {kind}", title
    return q
