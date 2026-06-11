from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import openaire
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

_ENT = "https://api.openaire.eu/graph/v1/researchProducts/doi_dedup___%3A%3A5c75a0e2"
_SX = "https://api.scholexplorer.openaire.eu/v3/Links?sourcePid=10.1101/844522"


def _oa_record(with_doi: bool = True, doi_in_instance: bool = False) -> dict:
    pids = (
        [{"scheme": "doi", "value": "10.1101/844522"}] if (with_doi and not doi_in_instance) else []
    )
    instances = [
        {
            "pids": [{"scheme": "doi", "value": "10.1101/844522"}] if doi_in_instance else [],
            "type": "article",
        }
    ]
    return {
        "id": "doi_dedup___::5c75a0e2",
        "mainTitle": "A comprehensive online database",
        "descriptions": ["<jats:title>Abstract</jats:title> <jats:p>NGS application.</jats:p>"],
        "authors": [{"fullName": "Zhang, Hong"}, {"fullName": "Li, Wei"}],
        "subjects": [{"subject": {"scheme": "FOS", "value": "Genomics"}}],
        "pids": pids,
        "instances": instances,
        "publicationDate": "2019-11-18",
        "type": "publication",
    }


def test_normalize_openaire_maps_core_fields_and_strips_jats() -> None:
    r = openaire._normalize_openaire(_oa_record())
    assert r.id == "openaire:doi_dedup___::5c75a0e2"
    assert r.source == "openaire"
    assert r.kind == "publication"
    assert r.title == "A comprehensive online database"
    assert r.creators == [Creator(name="Zhang, Hong"), Creator(name="Li, Wei")]
    assert r.year == 2019
    assert r.doi == "10.1101/844522"
    assert r.subjects == ["Genomics"]
    assert "<jats" not in (r.description or "")
    assert "Abstract" in r.description and "NGS application." in r.description


def test_normalize_openaire_doi_from_instance_fallback() -> None:
    r = openaire._normalize_openaire(_oa_record(doi_in_instance=True))
    assert r.doi == "10.1101/844522"


def test_normalize_openaire_without_doi() -> None:
    r = openaire._normalize_openaire(_oa_record(with_doi=False))
    assert r.doi is None


def test_normalize_openaire_tolerates_null_description() -> None:
    rec = _oa_record()
    rec["descriptions"] = [None]  # malformed null element must not crash
    assert openaire._normalize_openaire(rec).description is None


def test_normalize_openaire_tolerates_null_list_fields() -> None:
    # OpenAIRE serializes absent list fields as explicit null (not [] or omitted);
    # subjects/authors/instances must not crash iteration.
    rec = _oa_record()
    rec["subjects"] = None
    rec["authors"] = None
    rec["instances"] = None
    r = openaire._normalize_openaire(rec)
    assert r.subjects == []
    assert r.creators == []
    assert r.doi == "10.1101/844522"  # pids fallback still works with null instances


def test_normalize_sets_access_and_license() -> None:
    from data_aggregator_mcp import openaire

    rec = {
        "id": "oai:x",
        "mainTitle": "t",
        "bestAccessRight": {
            "code": "c_abf2",
            "label": "OPEN",
            "scheme": "http://vocabularies.coar-repositories.org/...",
        },
        "instances": [{"license": "CC BY"}],
    }
    r = openaire._normalize_openaire(rec)
    assert r.access == "open"
    assert r.license == "CC BY"


def test_normalize_access_closed_and_no_license() -> None:
    from data_aggregator_mcp import openaire

    rec = {
        "id": "oai:y",
        "mainTitle": "t",
        "bestAccessRight": {"code": "c_14cb", "label": "CLOSED"},
        "instances": [{"license": None}],
    }
    r = openaire._normalize_openaire(rec)
    assert r.access == "closed"
    assert r.license is None


def test_normalize_access_none_when_bestaccessright_absent() -> None:
    from data_aggregator_mcp import openaire

    r = openaire._normalize_openaire({"id": "oai:z", "mainTitle": "t"})
    assert r.access is None
    assert r.license is None


async def test_search_returns_publications(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts?search=ngs&type=publication&pageSize=10",
        json={"header": {"numFound": 213}, "results": [_oa_record()]},
    )
    async with httpx.AsyncClient() as client:
        total, results = await openaire.search(client, "ngs")
    assert total == 213
    assert [r.id for r in results] == ["openaire:doi_dedup___::5c75a0e2"]


async def test_resolve_fetches_entity_and_attaches_scholix_links(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    # resolve() now also enriches via idconv + full text; stub idconv to {} and
    # let the EuropePMC leg find no OA full text so this test isolates Scholix.
    async def _no_ids(client, doi):
        return {}

    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _no_ids)
    httpx_mock.add_response(url=_ENT, json=_oa_record())
    httpx_mock.add_response(
        url=_SX,
        json={
            "result": [
                {
                    "RelationshipType": {"Name": "IsSupplementedBy"},
                    "target": {
                        "Identifier": [{"ID": "10.5061/dryad.z", "IDScheme": "doi"}],
                        "Type": "dataset",
                    },
                }
            ]
        },
    )
    httpx_mock.add_response(
        url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:"10.1101/844522"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:doi_dedup___::5c75a0e2")
    assert r.id == "openaire:doi_dedup___::5c75a0e2"
    assert [(lnk.rel, lnk.target_id) for lnk in r.links] == [
        ("is_supplement_to", "datacite:10.5061/dryad.z")
    ]


async def test_resolve_no_links_when_no_doi(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_ENT, json=_oa_record(with_doi=False))
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:doi_dedup___::5c75a0e2")
    assert r.links == []  # no DOI → no Scholix query


async def test_resolve_unroutable_prefix_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="unroutable"):
            await openaire.resolve(client, "pubmed:1")


async def test_resolve_falls_back_to_request_id_when_record_id_blank(httpx_mock: HTTPXMock) -> None:
    rec = _oa_record(with_doi=False)
    rec["id"] = ""  # single-entity payload blanks/omits its own id
    httpx_mock.add_response(url=_ENT, json=rec)
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:doi_dedup___::5c75a0e2")
    assert (
        r.id == "openaire:doi_dedup___::5c75a0e2"
    )  # falls back to the request id, not "openaire:"


@pytest.mark.asyncio
async def test_search_offset_requests_page_and_slices():
    captured = {}

    async def handler(request):
        captured.update(dict(request.url.params))
        results = [{"id": str(i), "title": f"t{i}"} for i in range(10)]
        return httpx.Response(200, json={"header": {"numFound": 100}, "results": results})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        total, recs = await openaire.search(client, "q", size=10, offset=10)
    assert captured["page"] == "2"  # 10//10 + 1
    assert len(recs) == 10


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_openaire_search_normalizes() -> None:
    async with httpx.AsyncClient() as client:
        total, results = await openaire.search(client, "Phelipanche aegyptiaca", size=5)
    assert total >= 1
    assert results and all(r.source == "openaire" and r.kind == "publication" for r in results)


@live_only
async def test_live_openaire_resolve_fetches_entity() -> None:
    # Confirmed live id (2026-05-28). Exercises the real single-entity fetch +
    # Scholix link path (the search live test does not). This paper's Scholix
    # edges are citations, so links[] may be empty — assert the mechanism, not yield.
    oid = "openaire:doi_dedup___::5c75a0e2dec313cce0be5e1b16051d60"
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, oid)
    assert r.source == "openaire" and r.kind == "publication"
    assert all(lnk.target_id.startswith("datacite:") for lnk in r.links)


async def test_resolve_attaches_identifiers_and_fulltext(httpx_mock, monkeypatch) -> None:
    # Stub Scholix links + idconv to isolate this test's assertions.
    async def _no_scholix(client, doi):
        return []

    async def _ids(client, doi):
        return {"doi": doi, "pmid": "23066504", "pmcid": "PMC3463246"}

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_scholix)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _ids)
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts/oai123",
        json={
            "id": "oai123",
            "mainTitle": "t",
            "type": "publication",
            "pids": [{"scheme": "doi", "value": "10.7554/eLife.00013"}],
        },
    )
    httpx_mock.add_response(
        url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:"PMC3463246"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "Y", "pmcid": "PMC3463246"}]}},
    )
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:oai123")
    assert r.identifiers["pmcid"] == "PMC3463246"
    assert len(r.files) == 1 and r.files[0].source == "europepmc"


async def test_resolve_fills_access_license_from_fulltext_when_absent(
    httpx_mock, monkeypatch
) -> None:
    # OpenAIRE record carries no P8 bestAccessRight → access/license None; the EuropePMC
    # core record fills them. (P8 rights, when present, stay primary — see consumer guard.)
    async def _no_scholix(client, doi):
        return []

    async def _ids(client, doi):
        return {"doi": doi, "pmcid": "PMC3463246"}

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_scholix)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _ids)
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts/oai123",
        json={
            "id": "oai123",
            "mainTitle": "t",
            "type": "publication",
            "pids": [{"scheme": "doi", "value": "10.7554/eLife.00013"}],
        },
    )
    httpx_mock.add_response(
        url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:"PMC3463246"&format=json&resultType=core&pageSize=1',
        json={
            "resultList": {
                "result": [
                    {"inEPMC": "Y", "pmcid": "PMC3463246", "isOpenAccess": "Y", "license": "cc by"}
                ]
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:oai123")
    assert r.access == "open"
    assert r.license == "cc by"
