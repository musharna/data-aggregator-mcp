"""What the PubMed adapter sends and what it reports, pinned exactly (#88 mutation burn-down)."""

from __future__ import annotations

import logging

import httpx
import pytest

from data_aggregator_mcp import pubmed


def _efetch_answering(body: str, sent: list[httpx.Request]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text=body)

    return httpx.MockTransport(answer)


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)


async def test_the_abstract_is_every_non_blank_abstract_text_joined_by_one_space() -> None:
    sent: list[httpx.Request] = []
    body = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>"
        "<AbstractText> First part. </AbstractText>"
        "<AbstractText>   </AbstractText>"
        '<AbstractText Label="METHODS">Second part.</AbstractText>'
        "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    async with httpx.AsyncClient(transport=_efetch_answering(body, sent)) as client:
        assert await pubmed._abstract_for(client, "42") == ("First part. Second part.", None)
    assert [(r.method, r.url.path, dict(r.url.params)) for r in sent] == [
        ("GET", "/entrez/eutils/efetch.fcgi", {"db": "pubmed", "id": "42", "retmode": "xml"})
    ]


async def test_an_article_without_an_abstract_has_none_and_no_error() -> None:
    sent: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=_efetch_answering("<PubmedArticleSet/>", sent)
    ) as client:
        assert await pubmed._abstract_for(client, "42") == (None, None)


async def test_a_failed_abstract_fetch_is_logged_and_named(monkeypatch, caplog) -> None:
    async def failing_efetch(*_a, **_k) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(pubmed._eutils, "efetch", failing_efetch)
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.pubmed")
    async with httpx.AsyncClient() as client:
        result = await pubmed._abstract_for(client, "42")
    assert result == (None, "NCBI efetch (abstract) failed: RuntimeError: boom")
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            "data_aggregator_mcp.pubmed",
            logging.WARNING,
            "pubmed abstract fetch failed for '42': RuntimeError('boom')",
        )
    ]


async def test_an_abstract_that_is_not_xml_is_named_as_a_parse_failure(monkeypatch) -> None:
    async def text_efetch(*_a, **_k) -> str:
        return "not xml"

    monkeypatch.setattr(pubmed._eutils, "efetch", text_efetch)
    async with httpx.AsyncClient() as client:
        result = await pubmed._abstract_for(client, "42")
    assert result == (
        None,
        "NCBI efetch (abstract) failed: ParseError: syntax error: line 1, column 0",
    )
