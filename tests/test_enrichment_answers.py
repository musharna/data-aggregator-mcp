"""A broken enrichment answer is a named failure, never an empty answer (#88 burn-down
of `fulltext`, `idconv`, `scholix`).

All three read ``resp.json()`` themselves instead of through ``_http.request_json``, so a
body off their contract was read as "no open-access copy", "not in PMC" or "no data
links", and Scholix let an outage or a JSON list escape and sink the resolve it only
enriches. Each case below sits next to the real answer it must not be confused with;
the shapes of those answers were taken from the live services on 2026-10-01.
"""

from __future__ import annotations

import json

import httpx
import pytest

from data_aggregator_mcp import _http, fulltext, idconv, openaire, scholix


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


def _client(status: int, body: object) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


# EuropePMC answered a request with pageSize=-5 with exactly this 200.
_EPMC_ERROR = {"errCode": 404, "errMsg": "Invalid page size provided."}


async def test_a_europepmc_error_answer_is_a_failure_not_no_copy():
    async with _client(200, {"resultList": {"result": []}}) as c:  # the real no-hit
        assert await fulltext._europepmc(c, "PMC1", None) == fulltext.FullText()
    async with _client(200, _EPMC_ERROR) as c:
        ft = await fulltext._europepmc(c, "PMC1", None)
    assert ft.file is None
    assert ft.error is not None
    assert ft.error.startswith("EuropePMC lookup failed: UpstreamUnavailableError: ")
    assert "Invalid page size provided." in ft.error


@pytest.mark.parametrize("body", [{"status": "error", "message": "x"}, {"records": None}])
async def test_an_idconv_answer_without_a_record_is_a_failure_not_absent_from_pmc(body):
    # idconv's real answer for a DOI it cannot convert: a record, status "error".
    no_hit = {"records": [{"doi": "10.9/x", "requested-id": "10.9/x", "status": "error"}]}
    async with _client(200, no_hit) as c:
        assert await idconv.identifiers_for(c, "10.9/x") == ({}, None)
    async with _client(200, body) as c:
        ids, reason = await idconv.identifiers_for(c, "10.9/x")
    assert ids == {}
    assert reason is not None
    assert reason.startswith("NCBI idconv lookup failed: UpstreamUnavailableError: ")
    assert "no records list of objects" in reason


@pytest.mark.parametrize(
    ("status", "body", "cause"),
    [
        (503, None, "ScholeXplorer exhausted 3 retries (last HTTP 503)"),
        (200, [], "expected dict JSON, got list"),
        (200, {"error": "x"}, "no result list of link objects"),
        (200, {"result": [{"target": None}]}, "no result list of link objects"),
    ],
)
async def test_a_failed_scholix_answer_degrades_to_a_reason(status, body, cause):
    async with _client(200, {"result": []}) as c:  # the real answer for a PID with no links
        assert await scholix.links_for(c, "10.9/x") == ([], None)
    async with _client(status, body) as c:
        links, reason = await scholix.links_for(c, "10.9/x")
    assert links == []
    assert reason is not None
    assert reason.startswith("ScholeXplorer lookup failed (UpstreamUnavailableError: ")
    assert reason.endswith("); data links unknown")
    assert cause in reason


async def test_a_scholix_outage_leaves_the_openaire_record_resolved_and_says_so():
    """It raised out of ``openaire.resolve``, losing a record OpenAIRE had answered."""
    record = {"id": "oa1", "mainTitle": "A paper", "pids": [{"scheme": "doi", "value": "10.9/x"}]}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.scholexplorer.openaire.eu":
            return httpx.Response(503)
        if request.url.host == "www.ncbi.nlm.nih.gov":
            return httpx.Response(200, json={"records": [{"doi": "10.9/x", "status": "error"}]})
        if request.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": []}})
        return httpx.Response(200, json=record)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as c:
        r = await openaire.resolve(c, "openaire:oa1")
    assert r.title == "A paper"
    assert r.doi == "10.9/x"
    assert r.errors == {
        "links": "ScholeXplorer lookup failed (UpstreamUnavailableError: "
        "[UpstreamUnavailableError] ScholeXplorer exhausted 3 retries (last HTTP 503)); "
        "data links unknown"
    }


async def test_an_unpaywall_location_that_is_not_an_object_is_a_failure(monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    good = {"is_oa": True, "best_oa_location": {"url_for_pdf": "https://r/x.pdf"}}
    async with _client(200, good) as c:
        assert (await fulltext._unpaywall(c, "10.9/x")).file is not None
    async with _client(200, {"is_oa": True, "best_oa_location": "https://r/x.pdf"}) as c:
        ft = await fulltext._unpaywall(c, "10.9/x")
    assert (ft.file, ft.access) == (None, None)
    assert ft.error is not None
    assert "best_oa_location is not an object" in ft.error
