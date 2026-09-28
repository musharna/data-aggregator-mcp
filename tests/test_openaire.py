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
        return {}, None

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
        url="https://doi.org/ra/10.5061/dryad.z",
        json=[{"DOI": "10.5061/dryad.z", "RA": "DataCite"}],
    )
    httpx_mock.add_response(
        url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:"10.1101/844522"&format=json&resultType=core&pageSize=1',
        json={"resultList": {"result": [{"inEPMC": "N"}]}},
    )
    async with httpx.AsyncClient() as client:
        r = await openaire.resolve(client, "openaire:doi_dedup___::5c75a0e2")
    assert r.id == "openaire:doi_dedup___::5c75a0e2"
    assert [(lnk.rel, lnk.target_id) for lnk in r.links] == [
        # paper IsSupplementedBy dataset: direction kept (L23, audit 2026-09-22)
        ("is_supplemented_by", "datacite:10.5061/dryad.z")
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
        return [], None

    async def _ids(client, doi):
        return {"doi": doi, "pmid": "23066504", "pmcid": "PMC3463246"}, None

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
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:PMC3463246&format=json&resultType=core&pageSize=1",
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
        return [], None

    async def _ids(client, doi):
        return {"doi": doi, "pmcid": "PMC3463246"}, None

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
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:PMC3463246&format=json&resultType=core&pageSize=1",
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


async def test_resolve_records_a_failed_link_label_lookup_and_is_not_cached(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """When the doi.org registration-agency lookup fails, the Scholix data link still
    comes back (bare), but the record says so in errors["links"] and router.resolve does
    not cache it — the next resolve retries. A silent bare DOI read exactly like "not a
    DataCite DOI" and was served from cache for the TTL."""
    from data_aggregator_mcp import _http, fulltext, router

    async def _no_sleep(*_a, **_k):
        return None

    async def _no_ids(client, doi):
        return {}, None

    async def _no_fulltext(client, pmcid=None, doi=None):
        return fulltext.FullText()

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _no_ids)
    monkeypatch.setattr("data_aggregator_mcp.fulltext.find", _no_fulltext)
    for oid, doi, data_doi in (
        ("oaibad", "10.1/bad", "10.5061/dryad.bad"),
        ("oaiok", "10.1/ok", "10.5061/dryad.ok"),
    ):
        httpx_mock.add_response(
            url=f"https://api.openaire.eu/graph/v1/researchProducts/{oid}",
            json={
                "id": oid,
                "mainTitle": "t",
                "type": "publication",
                "pids": [{"scheme": "doi", "value": doi}],
            },
            is_reusable=True,
        )
        httpx_mock.add_response(
            url=f"https://api.scholexplorer.openaire.eu/v3/Links?sourcePid={doi}",
            json={
                "result": [
                    {
                        "RelationshipType": {"Name": "IsSupplementedBy"},
                        "target": {
                            "Identifier": [{"ID": data_doi, "IDScheme": "doi"}],
                            "Type": "dataset",
                        },
                    }
                ]
            },
            is_reusable=True,
        )
    httpx_mock.add_response(
        url="https://doi.org/ra/10.5061/dryad.bad",
        text="<html><body>502 Bad Gateway</body></html>",
        headers={"Content-Type": "text/html"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://doi.org/ra/10.5061/dryad.ok",
        json=[{"DOI": "10.5061/dryad.ok", "RA": "DataCite"}],
    )
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        bad = await router.resolve(client, "openaire:oaibad")
        await router.resolve(client, "openaire:oaibad")
        ok = await router.resolve(client, "openaire:oaiok")
        await router.resolve(client, "openaire:oaiok")
    entity_gets = [str(r.url).rsplit("/", 1)[-1] for r in httpx_mock.get_requests()]
    assert [lnk.target_id for lnk in bad.links] == ["10.5061/dryad.bad"]
    assert "registration-agency lookup failed" in bad.errors["links"]
    assert entity_gets.count("oaibad") == 2  # not cached: the second resolve re-fetched
    # Positive control: a lookup that answers labels the link, reports nothing, IS cached.
    assert [lnk.target_id for lnk in ok.links] == ["datacite:10.5061/dryad.ok"]
    assert ok.errors == {} and entity_gets.count("oaiok") == 1


async def test_resolve_records_failed_idconv_and_fulltext_lookups_and_is_not_cached(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """idconv and the EuropePMC full-text check are enrichment. A failure of either came
    back as {} / no file, the same values as "not in PMC" / "no open-access copy", and
    router.resolve cached the record for the TTL. Each failure is now named in errors,
    which keeps the record out of the cache; a record whose lookups answered is cached."""
    from data_aggregator_mcp import _http, router

    async def _no_sleep(*_a, **_k):
        return None

    async def _no_scholix(client, doi):
        return [], None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_scholix)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    for oid, doi in (("oaibad", "10.1/bad"), ("oaiok", "10.1/ok")):
        httpx_mock.add_response(
            url=f"https://api.openaire.eu/graph/v1/researchProducts/{oid}",
            json={
                "id": oid,
                "mainTitle": "t",
                "type": "publication",
                "pids": [{"scheme": "doi", "value": doi}],
            },
            is_reusable=True,
        )
    html = {
        "text": "<html><body>502 Bad Gateway</body></html>",
        "headers": {"Content-Type": "text/html"},
    }
    idc = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={}&format=json&tool=data-aggregator-mcp"
    epmc = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        "?query={}&format=json&resultType=core&pageSize=1"
    )
    httpx_mock.add_response(url=idc.format("10.1/bad"), is_reusable=True, **html)
    httpx_mock.add_response(url=epmc.format('DOI:"10.1/bad"'), is_reusable=True, **html)
    httpx_mock.add_response(
        url=idc.format("10.1/ok"), json={"records": [{"doi": "10.1/ok", "pmcid": "PMC7"}]}
    )
    httpx_mock.add_response(
        url=epmc.format("PMCID:PMC7"),
        json={"resultList": {"result": [{"inEPMC": "Y", "pmcid": "PMC7"}]}},
    )
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        bad = await router.resolve(client, "openaire:oaibad")
        await router.resolve(client, "openaire:oaibad")
        ok = await router.resolve(client, "openaire:oaiok")
        await router.resolve(client, "openaire:oaiok")
    entity_gets = [
        r.url.path.rsplit("/", 1)[-1]
        for r in httpx_mock.get_requests()
        if r.url.host == "api.openaire.eu"
    ]
    assert bad.identifiers == {} and "NCBI idconv" in bad.errors["identifiers"]
    assert bad.files == [] and "EuropePMC" in bad.errors["files"]
    assert entity_gets.count("oaibad") == 2  # not cached: the second resolve re-fetched
    # Positive control: lookups that answer fill the record, report nothing, ARE cached.
    assert ok.identifiers["pmcid"] == "PMC7" and [f.source for f in ok.files] == ["europepmc"]
    assert ok.errors == {} and entity_gets.count("oaiok") == 1
