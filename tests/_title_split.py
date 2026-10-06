"""What a plain-words search sends Zenodo and DataCite: two halves that split the
query by title (test_title_tier.py), the records naming it in a title, then the rest."""

from __future__ import annotations

import httpx

from data_aggregator_mcp._relevance import title_clause, with_plurals

_HALF = {
    "zenodo": ("https://zenodo.org/api/records", "q", "title", {}),
    "datacite": ("https://api.datacite.org/dois", "query", "titles.title", {"sort": "relevance"}),
}
_EMPTY = {
    "zenodo": {"hits": {"total": 0, "hits": []}},
    "datacite": {"data": [], "meta": {"total": 0}},
}


def half_urls(source: str, query: str, size: int = 10) -> list[str]:
    """The title half's URL, then the rest's, as the adapter sends them for ``query``."""
    base, param, field, extra = _HALF[source]
    words = with_plurals(query)
    clause = title_clause(query, field)
    size_key = "size" if source == "zenodo" else "page[size]"
    return [
        str(
            httpx.URL(
                base, params={param: f"({words}) AND {how}{clause}", **extra, size_key: str(size)}
            )
        )
        for how in ("", "NOT ")
    ]


def mock_halves(
    httpx_mock, source: str, query: str, *, size: int = 10, json=None, rest=None
) -> None:
    """``json`` answers the title half and ``rest`` the other; either left out is empty."""
    title_url, rest_url = half_urls(source, query, size)
    httpx_mock.add_response(url=title_url, json=json if json is not None else _EMPTY[source])
    httpx_mock.add_response(url=rest_url, json=rest if rest is not None else _EMPTY[source])


def title_half(query: str) -> bool:
    """Whether ``query`` is the title half of a split search."""
    return " AND title:" in query or " AND titles.title:" in query


def whole(sent: list[str]) -> str:
    """The query a source was sent. A plain-words search sends a source with a title
    field two halves that split it: ``(q) AND <field>:(...)``, then ``(q) AND NOT`` the
    same clause; both are checked to wrap the same ``q``. (The clause holds no " AND ":
    a query with an operator is never split.)"""
    if len(sent) == 1:
        return sent[0]
    title, rest = sent
    cut = title.rindex(" AND ")
    q, clause = title[1 : cut - 1], title[cut + 5 :]
    assert clause.startswith(("title:", "titles.title:")), clause
    assert title == f"({q}) AND {clause}" and rest == f"({q}) AND NOT {clause}"
    return q
