"""What trust.annotate sends to Crossref, which DOIs it checks, and what it logs."""

import logging

import httpx
import pytest

from data_aggregator_mcp import _http, trust
from data_aggregator_mcp.models import DataResource, Link

_CLEAN = {"message": {"DOI": "10.1234/w"}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _resource(doi=None, *, ident=None, links=()):
    r = DataResource(id="x:1", source="x", kind="dataset", title="t", doi=doi, links=list(links))
    if ident:
        r.identifiers["doi"] = ident
    return r


def _paper(target: str, rel: str = "described_in") -> Link:
    return Link(rel=rel, target_id=target)


async def _annotate(resource, answer=lambda doi: (200, _CLEAN)):
    """Annotate with Crossref answering ``answer(doi)``; return (signals, requests)."""
    from urllib.parse import unquote

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answer(unquote(request.url.path.removeprefix("/works/")))
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await trust.annotate(c, resource)
    return out, seen


def _dois(seen: list[httpx.Request]) -> list[str]:
    from urllib.parse import unquote

    return [unquote(r.url.path.removeprefix("/works/")) for r in seen]


@pytest.mark.asyncio
async def test_the_request_is_a_plain_get_of_the_work():
    out, seen = await _annotate(_resource("10.1038/nature14539"))
    assert out.retracted is False
    (req,) = seen
    assert req.method == "GET"
    assert str(req.url) == "https://api.crossref.org/works/10.1038/nature14539"
    assert req.headers["User-Agent"] == (
        "data-aggregator-mcp (+https://github.com/musharna/data-aggregator-mcp)"
    )
    assert req.headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_an_outage_is_tried_twice_and_logged_with_the_doi_and_the_error(caplog):
    caplog.set_level(logging.WARNING, logger="data_aggregator_mcp.trust")
    out, seen = await _annotate(_resource("10.1234/x"), lambda doi: (503, {}))
    assert (out.retracted, out.concern) == (None, None)
    assert len(seen) == 2
    assert [r.getMessage() for r in caplog.records] == [
        "trust annotate failed for 10.1234/x: UpstreamUnavailableError("
        "'Crossref retraction exhausted 2 retries (last HTTP 503)')"
    ]
    # positive control: an answered lookup logs nothing
    caplog.clear()
    out, _ = await _annotate(_resource("10.1234/x"))
    assert out.retracted is False and caplog.records == []


@pytest.mark.asyncio
async def test_a_paper_crossref_does_not_register_leaves_the_verdict_to_the_others():
    """A 404 is "not a Crossref work", not a failed lookup: a DataCite data DOI beside a
    clean paper is clean, while an outage on that DOI makes the verdict unknown."""
    record = _resource("10.5061/dryad.x", links=[_paper("10.1234/paper")])

    def answer(missing):
        return lambda doi: (missing, {}) if doi == "10.5061/dryad.x" else (200, _CLEAN)

    out, seen = await _annotate(record, answer(404))
    assert (out.retracted, out.concern, len(seen)) == (False, False, 2)
    out, _ = await _annotate(record, answer(503))
    assert (out.retracted, out.concern) == (None, None)


@pytest.mark.asyncio
async def test_a_retracted_record_keeps_a_concern_raised_on_another_doi():
    record = _resource("10.1234/own", links=[_paper("10.1234/paper")])
    retracted = {"message": {"DOI": "10.1234/own", "updated-by": [{"type": "retraction"}]}}
    concern = {
        "message": {"DOI": "10.1234/paper", "updated-by": [{"type": "expression_of_concern"}]}
    }

    def answer(paper):
        return lambda doi: (200, retracted) if doi == "10.1234/own" else paper

    out, _ = await _annotate(record, answer((200, concern)))
    assert (out.retracted, out.concern) == (True, True)
    out, _ = await _annotate(record, answer((200, _CLEAN)))
    assert (out.retracted, out.concern) == (True, False)
    out, _ = await _annotate(record, answer((503, {})))
    assert (out.retracted, out.concern) == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "10.1234/Paper",
        "  10.1234/Paper ",
        "doi:10.1234/Paper",
        "DOI:10.1234/Paper",
        "https://doi.org/10.1234/Paper",
        "HTTPS://DOI.ORG/10.1234/Paper",
        "http://doi.org/10.1234/Paper",
        "https://dx.doi.org/10.1234/Paper",
    ],
)
async def test_a_described_in_paper_is_checked_in_every_doi_form(target):
    _, seen = await _annotate(_resource("10.1234/own", links=[_paper(target)]))
    assert _dois(seen) == ["10.1234/own", "10.1234/Paper"]


@pytest.mark.asyncio
async def test_only_a_described_in_doi_is_checked():
    links = [
        _paper("plant-genomics:taxid:4081"),
        _paper("10.1234/cites", rel="cites"),
        _paper("https://www.rcsb.org/structure/7MWH", rel="landing_page"),
        _paper("10.1234/paper"),
    ]
    _, seen = await _annotate(_resource("10.1234/own", links=links))
    assert _dois(seen) == ["10.1234/own", "10.1234/paper"]


@pytest.mark.asyncio
async def test_each_doi_is_checked_once_whatever_its_case():
    links = [_paper("10.1234/ABC"), _paper("https://doi.org/10.1234/abc"), _paper("10.1234/Other")]
    _, seen = await _annotate(_resource("10.1234/abc", links=links))
    assert _dois(seen) == ["10.1234/abc", "10.1234/Other"]


@pytest.mark.asyncio
async def test_the_record_doi_comes_before_the_identifiers_doi():
    _, seen = await _annotate(_resource("10.1234/doi", ident="10.1234/ident"))
    assert _dois(seen) == ["10.1234/doi"]
    _, seen = await _annotate(_resource(None, ident="10.1234/ident"))
    assert _dois(seen) == ["10.1234/ident"]
    # a record with no DOI anywhere asks nothing and is unknown
    out, seen = await _annotate(_resource(None, links=[_paper("not-a-doi")]))
    assert (out.retracted, seen) == (None, [])
