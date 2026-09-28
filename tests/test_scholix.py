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


def _ra_datacite(httpx_mock: HTTPXMock, *dois: str) -> None:
    """doi.org RA answer registering every DOI with DataCite."""
    httpx_mock.add_response(
        url="https://doi.org/ra/" + ",".join(dois),
        json=[{"DOI": d, "RA": "DataCite"} for d in dois],
    )


async def test_links_for_maps_dataset_target_to_datacite(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_URL,
        json={"totalLinks": 1, "result": [_rec("dataset", "10.1594/PANGAEA.1")]},
    )
    _ra_datacite(httpx_mock, "10.1594/PANGAEA.1")
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    assert len(links) == 1
    assert links[0].target_id == "datacite:10.1594/PANGAEA.1"
    # source IsSupplementedBy target: the dataset supplements the queried record
    assert links[0].rel == "is_supplemented_by"


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
    _ra_datacite(httpx_mock, "10.5281/zenodo.9")
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


@pytest.mark.parametrize(
    ("scholix_rel", "ours"),
    [
        ("IsSupplementedBy", "is_supplemented_by"),
        ("IsSupplementTo", "is_supplement_to"),
        ("References", "references"),
        ("IsReferencedBy", "is_referenced_by"),
        ("IsRelatedTo", "is_related_to"),
        ("SomethingNew", "is_related_to"),  # unknown stays the neutral rel
    ],
)
async def test_links_for_keeps_relation_direction(
    httpx_mock: HTTPXMock, scholix_rel: str, ours: str
) -> None:
    """L23 (audit 2026-09-22): each inverse pair collapsed onto ONE rel, so "the paper IS
    SUPPLEMENTED BY this dataset" and "the paper IS A SUPPLEMENT TO this dataset" both
    read ``is_supplement_to`` — the direction of the edge, relative to the queried DOI,
    was lost (and for IsSupplementedBy, inverted). Same for References/IsReferencedBy.
    Rels are named in the DataCite snake-case vocabulary the rest of ``links`` uses."""
    httpx_mock.add_response(
        url=_URL, json={"totalLinks": 1, "result": [_rec("dataset", "10.1/d", rel=scholix_rel)]}
    )
    _ra_datacite(httpx_mock, "10.1/d")
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    assert [(lnk.rel, lnk.target_id) for lnk in links] == [(ours, "datacite:10.1/d")]


async def test_links_for_keeps_only_data_targets_and_labels_by_registration_agency(
    httpx_mock: HTTPXMock,
) -> None:
    """X-H2 (audit 2026-09-27): Scholix v3 types papers ``publication`` (never the
    ``literature`` the drop-list named), so every citation edge survived and was
    force-labelled ``datacite:`` — the Phelipanche paper (10.3390/plants13060869) got 9
    "data links", all Crossref journal articles. Keep dataset/software targets only, and
    claim ``datacite:`` only for DOIs DataCite registered (doi.org RA API)."""
    httpx_mock.add_response(
        url=_URL,
        json={
            "totalLinks": 5,
            "result": [
                _rec("publication", "10.1186/gb-2010-11-2-r14", rel="IsRelatedTo"),
                _rec("literature", "10.1093/nar/gky1", rel="IsRelatedTo"),
                _rec("other", "10.1234/other.1", rel="IsRelatedTo"),
                _rec("dataset", "10.5061/dryad.t4b8gtjgj"),
                _rec("dataset", "10.1016/j.dib.2020.1"),  # a Crossref-registered dataset
            ],
        },
    )
    httpx_mock.add_response(
        url="https://doi.org/ra/10.5061/dryad.t4b8gtjgj,10.1016/j.dib.2020.1",
        json=[
            {"DOI": "10.5061/dryad.t4b8gtjgj", "RA": "DataCite"},
            {"DOI": "10.1016/j.dib.2020.1", "RA": "Crossref"},
        ],
    )
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    assert [lnk.target_id for lnk in links] == [
        "datacite:10.5061/dryad.t4b8gtjgj",  # positive control: a DataCite dataset
        "10.1016/j.dib.2020.1",  # data, but not DataCite's: the bare DOI claims no agency
    ]


async def test_registration_agency_outage_keeps_data_links_as_bare_dois(
    httpx_mock: HTTPXMock,
) -> None:
    """The doi.org RA lookup only decides the ``datacite:`` label. When it fails (here an
    HTML error page in place of JSON), the data links must still come back — bare, since
    a bare DOI claims no agency — instead of the lookup failure sinking the whole
    OpenAIRE resolve that Scholix links merely enrich."""
    httpx_mock.add_response(
        url=_URL,
        json={
            "totalLinks": 2,
            "result": [
                _rec("dataset", "10.5061/dryad.t4b8gtjgj"),
                _rec("publication", "10.1186/gb-2010-11-2-r14", rel="IsRelatedTo"),
            ],
        },
    )
    httpx_mock.add_response(
        url="https://doi.org/ra/10.5061/dryad.t4b8gtjgj",
        text="<html><body>502 Bad Gateway</body></html>",
        headers={"Content-Type": "text/html"},
        is_reusable=True,  # the shared HTTP layer retries a non-JSON 200
    )
    async with httpx.AsyncClient() as client:
        links = await scholix.links_for(client, "10.5061/dryad.x")
    # Positive control inside: the data target survives and the citation is still dropped.
    assert [lnk.target_id for lnk in links] == ["10.5061/dryad.t4b8gtjgj"]


@live_only
async def test_live_citation_only_paper_yields_no_data_links() -> None:
    """X-H2 live: every ScholeXplorer edge of 10.3390/plants13060869 is a publication."""
    async with httpx.AsyncClient() as client:
        assert await scholix.links_for(client, "10.3390/plants13060869") == []
        pangaea = await scholix.links_for(client, "10.1594/PANGAEA.745671")  # control
    assert pangaea and all(lnk.target_id.startswith("datacite:") for lnk in pangaea)
