from __future__ import annotations

import json
import os

import httpx
import pytest

from data_aggregator_mcp import citation
from data_aggregator_mcp.models import Creator, DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_DOI_REC = DataResource(
    id="datacite:10.1038/x", source="datacite", kind="publication", title="T", doi="10.1038/x"
)
_NO_DOI = DataResource(
    id="geo:GSE1",
    source="omics",
    kind="dataset",
    title="My Study",
    creators=[Creator(name="Doe, Jane")],
    year=2021,
)


def test_accept_for_known_and_style() -> None:
    assert citation._accept_for("bibtex") == "application/x-bibtex"
    assert citation._accept_for("ris") == "application/x-research-info-systems"
    assert citation._accept_for("csl-json") == "application/vnd.citationstyles.csl+json"
    assert citation._accept_for("apa") == "text/x-bibliography; style=apa"


async def test_render_doi_content_negotiation(httpx_mock) -> None:
    httpx_mock.add_response(url="https://doi.org/10.1038/x", text="  @article{x} ")
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, _DOI_REC, "bibtex")
    assert out == "@article{x}"  # stripped


async def test_render_doi_failure_returns_none(httpx_mock) -> None:
    httpx_mock.add_response(url="https://doi.org/10.1038/x", status_code=404)
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, _DOI_REC, "nonsense-style")
    assert out is None  # fail soft, no raise


async def test_render_doi_transport_error_fails_soft(httpx_mock) -> None:
    for _ in range(3):  # request_with_retry retries transport errors max_retries times
        httpx_mock.add_exception(httpx.ConnectError("boom"), url="https://doi.org/10.1038/x")
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, _DOI_REC, "bibtex")
    assert out is None  # transport error must not raise out of enrichment


async def test_render_malformed_doi_fails_soft() -> None:
    # A non-printable char in an upstream-supplied DOI makes httpx raise InvalidURL at
    # request-build time. InvalidURL is NOT an httpx.HTTPError subclass, so a typed catch
    # would let it escape — the enrichment contract (spec §8) requires fail-soft regardless.
    bad = DataResource(
        id="datacite:bad", source="datacite", kind="publication", title="T", doi="10.1\x00bad/y"
    )
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, bad, "bibtex")
    assert out is None  # must not raise out of enrichment


async def test_render_non_doi_csl_json_from_metadata() -> None:
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, _NO_DOI, "csl-json")
    obj = json.loads(out)
    assert obj["title"] == "My Study"
    assert obj["type"] == "dataset"
    assert obj["author"] == [{"literal": "Doe, Jane"}]
    assert obj["issued"] == {"date-parts": [[2021]]}


async def test_render_non_doi_non_csl_returns_none() -> None:
    async with httpx.AsyncClient() as client:
        out = await citation.render(client, _NO_DOI, "bibtex")
    assert out is None  # bibtex/ris/styles need a DOI


@live_only
async def test_live_render_crossref_bibtex_and_csl() -> None:
    rec = DataResource(
        id="x", source="datacite", kind="publication", title="t", doi="10.1038/171737a0"
    )
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        bib = await citation.render(client, rec, "bibtex")
        csl = await citation.render(client, rec, "csl-json")
    assert bib and "@article" in bib.lower()
    assert json.loads(csl)["DOI"].lower() == "10.1038/171737a0"
