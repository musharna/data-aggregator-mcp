"""What the GWAS Catalog adapter sends and how it reads what comes back, pinned exactly.

Live shapes: see ``test_gwas_answers.py``.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, gwas
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, Link
from tests.test_gwas_answers import _PUBLICATION, _RECORD, _SEARCH

_V2 = "https://www.ebi.ac.uk/gwas/rest/api/v2"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(sent: list[httpx.Request], answer) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _routed(request: httpx.Request, *, study=_RECORD, publication=_PUBLICATION):
    if request.url.path.startswith("/gwas/rest/api/v2/publications/"):
        return httpx.Response(200, json=publication)
    if request.url.path.startswith("/gwas/rest/api/v2/studies/"):
        return httpx.Response(200, json=study)
    return httpx.Response(200, json=_SEARCH)


def _call(r: httpx.Request) -> tuple[str, str, str | None, float]:
    return r.method, str(r.url), r.headers.get("accept"), r.extensions["timeout"]["read"]


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_the_catalog_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, _routed) as c:
        await gwas.search(c, "Type 2 diabetes", size=7, offset=15)
        await gwas.search(c, "asthma")
        r = await gwas.resolve(c, "gwas: gcst000028 ")
    assert [_call(s) for s in sent] == [
        (
            "GET",
            f"{_V2}/studies?disease_trait=Type+2+diabetes&size=7&page=2",
            "application/json",
            60.0,
        ),
        ("GET", f"{_V2}/studies?disease_trait=asthma&size=10&page=0", "application/json", 60.0),
        ("GET", f"{_V2}/studies/GCST000028", "application/json", 60.0),
        ("GET", f"{_V2}/publications/17463246", "application/json", 60.0),
    ]
    assert r.id == "gwas:GCST000028" and r.year == 2007  # positive control: all read


@pytest.mark.asyncio
async def test_size_is_capped_and_the_page_counted_in_capped_rows():
    sent: list[httpx.Request] = []
    async with _recording(sent, _routed) as c:
        await gwas.search(c, "asthma", size=51, offset=149)
        await gwas.search(c, "asthma", size=50, offset=50)
    assert [dict(s.url.params) for s in sent] == [
        {"disease_trait": "asthma", "size": "50", "page": "2"},
        {"disease_trait": "asthma", "size": "50", "page": "1"},
    ]


@pytest.mark.asyncio
async def test_a_search_row_is_read_field_by_field():
    async with _recording([], _routed) as c:
        total, recs = await gwas.search(c, "asthma", size=2, offset=1)
    assert total == 87
    assert recs == [
        DataResource(
            id="gwas:GCST90476698",
            source="gwas",
            kind="study",
            title="Asthma",
            identifiers={"pmid": "39024449"},
            subjects=["Asthma"],
            links=[
                Link(
                    rel="landing_page", target_id="https://www.ebi.ac.uk/gwas/studies/GCST90476698"
                )
            ],
        )
    ]


@pytest.mark.asyncio
async def test_a_resolved_study_is_read_field_by_field():
    async with _recording([], _routed) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert r == DataResource(
        id="gwas:GCST000028",
        source="gwas",
        kind="study",
        title="Genome-wide association analysis identifies loci for type 2 diabetes and "
        "triglyceride levels.",
        year=2007,
        identifiers={"pmid": "17463246"},
        subjects=["Type 2 diabetes"],
        last_updated="2007-04-26",
        links=[Link(rel="landing_page", target_id="https://www.ebi.ac.uk/gwas/studies/GCST000028")],
    )


@pytest.mark.asyncio
async def test_a_study_with_no_pmid_or_trait_is_titled_by_its_accession():
    sent: list[httpx.Request] = []
    bare = {"accession_id": "GCST000028", "pubmed_id": None, "disease_trait": None}
    async with _recording(sent, lambda r: _routed(r, study=bare)) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert len(sent) == 1  # no PMID, so no publication request
    assert (r.title, r.identifiers, r.subjects, r.year, r.errors) == (
        "GCST000028",
        {},
        [],
        None,
        {},
    )
    # ... and a blank trait and pmid 0 count as none (positive control: the full study).
    blank = dict(bare, pubmed_id=0, disease_trait="")
    async with _recording(sent, lambda r: _routed(r, study=blank)) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert len(sent) == 2 and (r.title, r.identifiers, r.subjects) == ("GCST000028", {}, [])
    async with _recording(sent, _routed) as c:
        assert (await gwas.resolve(c, "gwas:GCST000028")).subjects == ["Type 2 diabetes"]


@pytest.mark.asyncio
async def test_a_publication_with_blank_fields_falls_back_to_the_trait():
    blank = dict(_PUBLICATION, title="", publication_date="")
    async with _recording([], lambda r: _routed(r, publication=blank)) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert (r.title, r.year, r.last_updated, r.errors) == ("Type 2 diabetes", None, None, {})
    undated = dict(_PUBLICATION, publication_date=None)
    async with _recording([], lambda r: _routed(r, publication=undated)) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert (r.year, r.last_updated) == (None, None) and r.title.startswith("Genome-wide")


@pytest.mark.asyncio
async def test_a_publication_the_catalog_lacks_is_not_an_error():
    missing = httpx.Response(404, json={"errorCode": 404, "error": "Not Found"})

    def answer(request):
        return missing if "/publications/" in request.url.path else _routed(request)

    async with _recording([], answer) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert (r.title, r.year, r.errors) == ("Type 2 diabetes", None, {})


@pytest.mark.asyncio
async def test_each_call_is_tried_three_times_and_named_in_its_error():
    sent: list[httpx.Request] = []

    def down(request):
        return httpx.Response(503, json={"status": 503})

    async with _recording(sent, down) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GWAS Catalog search exhausted 3 retries \(last HTTP 503\)$",
        ):
            await gwas.search(c, "asthma")
        assert len(sent) == 3
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GWAS Catalog resolve exhausted 3 retries \(last HTTP 503\)$",
        ):
            await gwas.resolve(c, "gwas:GCST000028")
        assert len(sent) == 6

    def pub_down(request):
        return down(request) if "/publications/" in request.url.path else _routed(request)

    async with _recording(sent, pub_down) as c:
        r = await gwas.resolve(c, "gwas:GCST000028")
    assert len(sent) == 10  # one study request, three publication tries
    assert r.title == "Type 2 diabetes" and r.errors == {
        "publication": "UpstreamUnavailableError: [UpstreamUnavailableError] GWAS Catalog publication exhausted 3 "
        "retries (last HTTP 503)"
    }


@pytest.mark.asyncio
async def test_a_search_404_is_not_zero_hits():
    def gone(request):
        return httpx.Response(404, json={"error": "Not Found"})

    async with _recording([], gone) as c:
        with pytest.raises(
            NotFoundError, match=r"^\[NotFoundError\] GWAS Catalog search → HTTP 404: "
        ):
            await gwas.search(c, "asthma")


@pytest.mark.asyncio
async def test_a_malformed_id_sends_nothing():
    sent: list[httpx.Request] = []
    async with _recording(sent, _routed) as c:
        for bad in ("gwas:", "gwas:GCST", "gwas:GCST12x", "gwas:XGCST12"):
            with pytest.raises(
                NotFoundError, match=rf"^\[NotFoundError\] malformed GWAS id '{bad}'$"
            ):
                await gwas.resolve(c, bad)
        assert sent == []
        await gwas.resolve(c, "gwas:GCST000028")  # positive control
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_mid_page_offset_drops_the_rows_already_returned():
    studies = [copy.deepcopy(_RECORD) for _ in range(3)]
    for i, s in enumerate(studies):
        s["accession_id"] = f"GCST{i}"
    body = {"_embedded": {"studies": studies}, "page": {"totalElements": 9}}
    async with _recording([], lambda r: httpx.Response(200, json=body)) as c:
        _, recs = await gwas.search(c, "x", size=3, offset=4)
    assert [r.id for r in recs] == ["gwas:GCST1", "gwas:GCST2"]
