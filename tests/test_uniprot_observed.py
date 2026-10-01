"""What uniprot sends and reports, pinned (#88 burn-down of `uniprot`).

The existing tests checked that a field was present and that a request reached the
host; 97 mutants survived: a name fallback, the landing link, the inactive-entry
message, and every parameter, header and timeout of both requests could change
unseen. Each test here drives the adapter against a server that records the request.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, uniprot
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_ENTRY = {
    "primaryAccession": "P01308",
    "uniProtkbId": "INS_HUMAN",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {"recommendedName": {"fullName": {"value": "Insulin"}}},
    "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
    "entryAudit": {"lastAnnotationUpdateDate": "2026-06-10"},
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


class _Server:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.mark.parametrize(
    ("entry", "title"),
    [
        ({"proteinDescription": {"submissionNames": [{"fullName": {}}]}}, "X_ARATH"),
        ({"uniProtkbId": None}, "Q9XYZ1"),
    ],
)
def test_the_title_falls_back_to_the_entry_name_then_the_accession(entry, title):
    base = {"primaryAccession": "Q9XYZ1", "uniProtkbId": "X_ARATH"}
    assert uniprot._normalize({**base, **entry}).title == title
    assert uniprot._protein_name({}) == ""


def test_normalize_keeps_the_update_date_and_links_the_landing_page():
    r = uniprot._normalize(_ENTRY)
    assert r.last_updated == "2026-06-10"
    assert [(link.rel, link.target_id) for link in r.links] == [
        ("landing_page", "https://www.uniprot.org/uniprotkb/P01308/entry")
    ]
    assert r.subjects == ["Homo sapiens", "Swiss-Prot"]


@pytest.mark.parametrize("entry_type", [None, "", "UniProtKB unknown"])
def test_an_entry_type_naming_no_curation_adds_no_subject(entry_type):
    """Positive control: the Swiss-Prot entry above gets its curation subject."""
    r = uniprot._normalize({**_ENTRY, "entryType": entry_type})
    assert r.subjects == ["Homo sapiens"]


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ({}, "UniProtKB entry P1 is inactive (unknown reason)"),
        (
            {"inactiveReasonType": "DELETED", "deletedReason": "Deleted from sequence source"},
            "UniProtKB entry P1 is inactive (DELETED): Deleted from sequence source",
        ),
        (
            {"inactiveReasonType": "MERGED", "mergeDemergeTo": ["P2", "P3"]},
            "UniProtKB entry P1 is inactive (MERGED); now uniprot:P2, uniprot:P3",
        ),
    ],
)
def test_the_inactive_message_names_the_reason_and_where_the_entry_went(reason, message):
    assert uniprot._inactive_message("P1", reason) == message


async def test_search_sends_the_query_as_json_capped_at_25_rows():
    server = _Server(
        httpx.Response(200, json={"results": [_ENTRY]}, headers={"x-total-results": "7"})
    )
    async with server.client() as c:
        assert (await uniprot.search(c, "insulin", size=100))[0] == 7
    (sent,) = server.requests
    assert sent.method == "GET"
    assert str(sent.url).startswith("https://rest.uniprot.org/uniprotkb/search?")
    assert dict(sent.url.params) == {"query": "insulin", "format": "json", "size": "25"}
    assert sent.headers["Accept"] == "application/json"
    assert sent.extensions["timeout"]["read"] == 30.0


async def test_resolve_sends_the_canonical_accession_as_json():
    server = _Server(httpx.Response(200, json=_ENTRY))
    async with server.client() as c:
        r = await uniprot.resolve(c, "uniprot: p01308 ")
    (sent,) = server.requests
    assert str(sent.url) == "https://rest.uniprot.org/uniprotkb/P01308?format=json"
    assert sent.headers["Accept"] == "application/json"
    assert sent.extensions["timeout"]["read"] == 30.0
    assert [(f.name, f.url, f.mime, f.source) for f in r.files] == [
        (
            "P01308.fasta",
            "https://rest.uniprot.org/uniprotkb/P01308.fasta",
            "text/x-fasta",
            "uniprot",
        )
    ]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            uniprot.search,
            "[UpstreamUnavailableError] UniProt search exhausted 2 retries (last HTTP 503)",
        ),
        (
            uniprot.resolve,
            "[UpstreamUnavailableError] UniProt resolve exhausted 2 retries (last HTTP 503)",
        ),
    ],
)
async def test_an_outage_is_retried_twice_and_named(call, message):
    server = _Server(httpx.Response(503))
    async with server.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await call(c, "uniprot:P01308")
    assert str(err.value) == message
    assert len(server.requests) == uniprot.MAX_RETRIES


async def test_resolve_names_the_entry_it_could_not_find():
    """A malformed id fails before the network; a 404 and an inactive stub name the
    canonical accession."""
    async with _Server(httpx.Response(404)).client() as c:
        with pytest.raises(NotFoundError) as err:
            await uniprot.resolve(c, "uniprot:p99999")
        assert str(err.value) == "[NotFoundError] UniProtKB has no entry P99999"
        with pytest.raises(NotFoundError) as err:
            await uniprot.resolve(c, "uniprot:P0/1")
        assert str(err.value) == "[NotFoundError] malformed UniProt id 'uniprot:P0/1'"
    stub = {"entryType": "Inactive", "inactiveReason": {"inactiveReasonType": "DELETED"}}
    async with _Server(httpx.Response(200, json=stub)).client() as c:
        with pytest.raises(NotFoundError) as err:
            await uniprot.resolve(c, "uniprot:q12345")
    assert str(err.value) == "[NotFoundError] UniProtKB entry Q12345 is inactive (DELETED)"
