"""What render sends to doi.org, what it logs, and the CSL-JSON it builds without a DOI."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from data_aggregator_mcp import citation
from data_aggregator_mcp.models import Creator, DataResource

_DOI = "10.1038/x"


def _rec(**update) -> DataResource:
    base = DataResource(id="pubmed:1", source="literature", kind="publication", title="T", doi=_DOI)
    return base.model_copy(update=update)


def _answering(sent: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("fmt", "accept"),
    [
        ("bibtex", "application/x-bibtex"),
        ("ris", "application/x-research-info-systems"),
        ("csl-json", "application/vnd.citationstyles.csl+json"),
        ("apa", "text/x-bibliography; style=apa"),
        (" Vancouver ", "text/x-bibliography; style=vancouver"),
    ],
)
async def test_one_get_to_doi_org_asks_for_the_format(fmt, accept) -> None:
    sent: list[httpx.Request] = []
    answer = httpx.Response(200, headers={"Content-Type": accept.partition(";")[0]}, text="c")
    async with _answering(sent, answer) as client:
        assert await citation.render(client, _rec(), fmt) == "c"
    [req] = sent
    assert req.method == "GET"
    assert str(req.url) == "https://doi.org/10.1038/x"
    assert req.headers["Accept"] == accept


async def test_a_blank_format_asks_nothing() -> None:
    sent: list[httpx.Request] = []
    answer = httpx.Response(200, headers={"Content-Type": "application/x-bibtex"}, text="c")
    async with _answering(sent, answer) as client:
        assert await citation.render(client, _rec(), " ") is None
        assert sent == []
        # Positive control: a format asks.
        assert await citation.render(client, _rec(), "bibtex") == "c"
    assert len(sent) == 1


async def test_a_format_that_needs_a_doi_says_so(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.citation")
    sent: list[httpx.Request] = []
    async with _answering(sent, httpx.Response(500)) as client:
        assert await citation.render(client, _rec(doi=None), "RIS") is None
    assert sent == []
    assert [r.getMessage() for r in caplog.records] == [
        "citation: format 'ris' needs a DOI; pubmed:1 has none"
    ]


async def test_a_malformed_doi_is_refused_by_name(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.citation")
    sent: list[httpx.Request] = []
    async with _answering(sent, httpx.Response(500)) as client:
        assert await citation.render(client, _rec(doi="10.1/a\x00b"), "bibtex") is None
    assert sent == []
    assert [r.getMessage() for r in caplog.records] == [
        "citation: DOI of pubmed:1 is malformed: '10.1/a\\x00b'"
    ]


async def test_a_failed_request_names_the_service_and_the_record(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.citation")
    sent: list[httpx.Request] = []
    async with _answering(sent, httpx.Response(404, text="DOI not found")) as client:
        assert await citation.render(client, _rec(), "bibtex") is None
    assert len(sent) == 1
    [msg] = [r.getMessage() for r in caplog.records]
    assert msg.startswith("citation render failed for pubmed:1 (bibtex): NotFoundError(")
    assert "DOI content negotiation" in msg


@pytest.mark.parametrize(
    ("kind", "csl_type"),
    [
        ("publication", "article-journal"),
        ("dataset", "dataset"),
        ("software", "software"),
        ("study", "dataset"),
        ("sequencing_run", "dataset"),
        ("other", "dataset"),
    ],
)
async def test_csl_json_without_a_doi_is_built_from_the_record(kind, csl_type) -> None:
    sent: list[httpx.Request] = []
    rec = _rec(doi=None, kind=kind, id="geo:GSE1", title="My Study")
    async with _answering(sent, httpx.Response(500)) as client:
        bare = await citation.render(client, rec, "csl-json")
        full = await citation.render(
            client,
            rec.model_copy(update={"creators": [Creator(name="Doe, J"), Creator(name="Roe")]}),
            "csl-json",
        )
        dated = await citation.render(client, rec.model_copy(update={"year": 2021}), "csl-json")
    assert sent == []
    head = {"id": "geo:GSE1", "type": csl_type, "title": "My Study"}
    assert json.loads(bare) == head
    assert json.loads(full) == {**head, "author": [{"literal": "Doe, J"}, {"literal": "Roe"}]}
    assert json.loads(dated) == {**head, "issued": {"date-parts": [[2021]]}}
