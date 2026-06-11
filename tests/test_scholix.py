from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import scholix

_URL = "https://api.scholexplorer.openaire.eu/v3/Links?sourcePid=10.5061/dryad.x"


def _rec(target_type: str, doi: str | None, rel: str = "IsSupplementedBy") -> dict:
    ident = [{"ID": doi, "IDScheme": "doi", "IDURL": None}] if doi else []
    ident.append({"ID": "50|abc", "IDScheme": "openaireIdentifier", "IDURL": None})
    return {
        "RelationshipType": {"Name": rel, "SubType": "", "SubTypeSchema": ""},
        "source": {"Identifier": [{"ID": "10.5061/dryad.x", "IDScheme": "doi"}]},
        "target": {"Identifier": ident, "Type": target_type, "Title": "t"},
    }


async def test_links_for_maps_dataset_target_to_datacite(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_URL,
        json={"totalLinks": 1, "result": [_rec("dataset", "10.1594/PANGAEA.1")]},
    )
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    assert len(links) == 1
    assert links[0].target_id == "datacite:10.1594/PANGAEA.1"
    assert links[0].rel == "is_supplement_to"


async def test_links_for_drops_literature_citation_edges(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_URL,
        json={
            "totalLinks": 2,
            "result": [
                _rec("literature", "10.1093/nar/gky1", rel="IsRelatedTo"),
                _rec("software", "10.5281/zenodo.9", rel="References"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    # literature (citation) dropped; software kept as datacite:
    assert [lnk.target_id for lnk in links] == ["datacite:10.5281/zenodo.9"]
    assert links[0].rel == "references"


async def test_links_for_skips_target_without_doi(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json={"totalLinks": 1, "result": [_rec("dataset", None)]})
    async with httpx.AsyncClient() as client:
        assert await scholix.links_for(client, "10.5061/dryad.x") == []


async def test_links_for_empty_doi_returns_empty() -> None:
    async with httpx.AsyncClient() as client:
        assert await scholix.links_for(client, None) == []
        assert await scholix.links_for(client, "") == []


async def test_links_for_404_returns_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, status_code=404)
    async with httpx.AsyncClient() as client:
        assert await scholix.links_for(client, "10.5061/dryad.x") == []


# ---------------------------------------------------------------------------
# Fix — links_for must degrade to [] on non-JSON body (e.g. HTML error page)
# ---------------------------------------------------------------------------


async def test_links_for_html_body_returns_empty(httpx_mock: HTTPXMock) -> None:
    """A 200 response with an HTML body (e.g. a WAF error page) must return []
    without raising — Scholix links are enrichment only."""
    httpx_mock.add_response(
        url=_URL,
        text="<html><body>Service Unavailable</body></html>",
        headers={"Content-Type": "text/html"},
    )
    async with httpx.AsyncClient() as client:
        result = await scholix.links_for(client, "10.5061/dryad.x")
    assert result == []


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_scholix_pangaea_returns_mappable_targets() -> None:
    # PANGAEA 10.1594/PANGAEA.745671 is densely linked; targets map to datacite:
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.1594/PANGAEA.745671")
    assert links  # non-empty
    assert all(lnk.target_id.startswith("datacite:") for lnk in links)
