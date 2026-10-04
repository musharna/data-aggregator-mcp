"""OpenAIRE answers HTTP 400 to a query with more than four AND/OR/NOT words, so an
ontology expansion that long lost the whole OpenAIRE stream. The router now sends it a
shortened expansion and says what it left out; every other source gets the full one.

Uses only names that existed before the fix, so it can be run against the old code."""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import AsyncMock

import httpx
import pytest

from data_aggregator_mcp import _cursor, anatomy, openaire, pubmed, router, taxonomy
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import DataResource

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"

_SKIN = anatomy.UberonInfo(
    uberon_id="UBERON:0000014",
    canonical="zone of skin",
    synonyms=("skin", "portion of skin", "region of skin", "skin region", "skin zone"),
)
_FULL = (
    '(single-cell) AND ("zone of skin" OR "skin" OR "portion of skin" OR "region of skin" '
    'OR "skin region" OR "skin zone")'
)
_SHORT = '(single-cell) AND ("skin" OR "zone of skin" OR "portion of skin" OR "region of skin")'
_NOTE = (
    "literature/openaire accepts at most 4 AND/OR/NOT operators, so it was searched with "
    "a shorter expansion that left out 'skin region', 'skin zone'"
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(599)))


def _serve_literature(monkeypatch) -> dict[str, list[tuple[str, int]]]:
    """Fake PubMed and OpenAIRE searches, each holding 10 records per query."""
    calls: dict[str, list[tuple[str, int]]] = {"pubmed": [], "openaire": []}

    def fake(name: str):
        async def search(client, query, *, size, offset=0):
            calls[name].append((query, offset))
            recs = [
                DataResource(id=f"{name}:{i}", source=name, kind="publication", title=f"t{i}")
                for i in range(offset, min(offset + size, 10))
            ]
            return 10, recs

        return search

    monkeypatch.setattr(pubmed, "search", fake("pubmed"))
    monkeypatch.setattr(openaire, "search", fake("openaire"))
    return calls


async def test_openaire_gets_a_shortened_expansion_and_pubmed_the_full_one(monkeypatch) -> None:
    monkeypatch.setattr(anatomy, "resolve_uberon", AsyncMock(return_value=_SKIN))
    calls = _serve_literature(monkeypatch)
    async with _client() as client:
        page = await router.search_page(
            client, query="single-cell", tissue="skin", sources=["literature"], size=4
        )
        nxt = await router.search_page(client, cursor=page.next_cursor)
    # Positive control: the uncapped backend still gets every synonym.
    assert calls["pubmed"][0] == (_FULL, 0)
    assert calls["openaire"][0] == (_SHORT, 0)
    assert page.errors == {"operator_limit": _NOTE}
    # Page 2 shortens the same way, from the groups the cursor carried.
    assert [q for q, _ in calls["openaire"]] == [_SHORT, _SHORT]
    assert [q for q, _ in calls["pubmed"]] == [_FULL, _FULL]
    assert nxt.errors == {"operator_limit": _NOTE}


async def test_an_expansion_within_the_limit_is_sent_unchanged(monkeypatch) -> None:
    liver = anatomy.UberonInfo(uberon_id="UBERON:0002107", canonical="liver", synonyms=("iecur",))
    monkeypatch.setattr(anatomy, "resolve_uberon", AsyncMock(return_value=liver))
    calls = _serve_literature(monkeypatch)
    async with _client() as client:
        page = await router.search_page(client, query="rna", tissue="liver", sources=["literature"])
    full = '(rna) AND ("liver" OR "iecur")'
    assert calls["openaire"] == calls["pubmed"] == [(full, 0)]
    assert page.errors == {}


async def test_too_many_facets_for_the_limit_fall_back_to_the_plain_query(monkeypatch) -> None:
    monkeypatch.setattr(anatomy, "resolve_uberon", AsyncMock(return_value=_SKIN))
    plant = taxonomy.TaxonInfo(taxid=4577, canonical_name="Zea mays", synonyms=(), is_plant=True)
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=plant))
    calls = _serve_literature(monkeypatch)
    query = "a AND b AND c AND d"  # three of OpenAIRE's four; two facets need two ANDs
    async with _client() as client:
        page = await router.search_page(
            client, query=query, organism="Zea mays", tissue="skin", sources=["literature"]
        )
    assert calls["openaire"] == [(query, 0)]
    assert calls["pubmed"][0][0].startswith("((a AND b AND c AND d) AND (")
    assert page.errors["operator_limit"] == (
        "literature/openaire accepts at most 4 AND/OR/NOT operators, too few for one name "
        "per facet, so it was searched with the plain query"
    )


async def test_every_multi_query_variant_is_shortened_and_noted_once(monkeypatch) -> None:
    monkeypatch.setattr(anatomy, "resolve_uberon", AsyncMock(return_value=_SKIN))
    monkeypatch.setattr(
        router.query_understanding_mod, "expand", AsyncMock(return_value=["scRNA-seq"])
    )
    calls = _serve_literature(monkeypatch)
    async with _client() as client:
        page = await router.search_page(
            client,
            query="single-cell",
            tissue="skin",
            sources=["literature"],
            multi_query=True,
            size=4,
        )
        nxt = await router.search_page(client, cursor=page.next_cursor)
    alt = _SHORT.replace("(single-cell)", "(scRNA-seq)")
    assert sorted({q for q, _ in calls["openaire"]}) == sorted({_SHORT, alt})
    assert _FULL in {q for q, _ in calls["pubmed"]}
    assert page.errors["operator_limit"] == _NOTE
    assert nxt.errors["operator_limit"] == _NOTE


def _token(state: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(state).encode()).decode()


@pytest.mark.parametrize(
    "fg",
    [
        "skin",
        [["skin"]],
        [["skin", "skin"]],
        [["skin", []]],
        [["skin", ["skin", 3]]],
        [[1, ["skin"]]],
        [["skin", ["skin"]]] * 6,
    ],
)
def test_a_cursor_refuses_facet_groups_no_search_could_mint(fg) -> None:
    good = {"q": "rna", "size": 10, "offsets": {}, "fg": [["skin", ["zone of skin", "skin"]]]}
    assert _cursor.decode(_token(good))["fg"] == good["fg"]
    with pytest.raises(ValidationError, match="'fg' must be a list of at most 5"):
        _cursor.decode(_token({**good, "fg": fg}))


@pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_openaire_answers_a_long_tissue_expansion() -> None:
    """Before the fix this search lost the OpenAIRE stream to HTTP 400."""
    async with httpx.AsyncClient(timeout=60) as client:
        page = await router.search_page(
            client, query="single-cell", tissue="skin", sources=["literature"], size=20
        )
    assert page.tissue_expansion is not None
    assert not any("openaire" in k for k in page.errors if k != "operator_limit"), page.errors
    assert "literature/openaire" in page.errors["operator_limit"]
    assert any(r.source == "openaire" for r in page.results)
    assert any(r.source == "pubmed" for r in page.results)
