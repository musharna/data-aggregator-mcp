"""PDB behaviour the mutation run (nightly-guardrails, #88) showed no test observed.

135 mutants survived test_pdb.py. Its mock answered any request the code sent, so the
search body, the GraphQL query, the headers, the retry budget and the error text were
never checked; and record fields were tested on fixtures that were already in order
(creators), had no grant number (funding) or no duplicate organism (taxa).

Fixture shapes follow RCSB's live answers (1BG2, 7XYZ-style provenance) as of 2026-09-30.
"""

from __future__ import annotations

import json

import httpx
import pytest

from data_aggregator_mcp import _http, pdb
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, FileEntry, FundingRef, Link, Taxon

_ENTRY = {
    "rcsb_id": "1BG2",
    "pdbx_database_status": {"pdb_format_compatible": "Y"},
    "struct": {"title": "Human kinesin motor domain"},
    "rcsb_accession_info": {"initial_release_date": "1998-10-14T00:00:00Z"},
    "rcsb_primary_citation": {
        "year": 1996,
        "pdbx_database_id_DOI": "10.1038/380550a0",
        "pdbx_database_id_PubMed": 8606779,
    },
    "rcsb_entry_info": {"experimental_method": "X-ray"},
    "database_2": [{"database_id": "PDB", "pdbx_DOI": "10.2210/pdb1bg2/pdb"}],
}
_OTHER = {**_ENTRY, "rcsb_id": "1GOJ", "struct": {"title": "Fast kinesin"}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


class _Server:
    """Answers search and GraphQL separately and records every request."""

    def __init__(self, search: httpx.Response | None = None, gql: object = None) -> None:
        self.search = search or httpx.Response(
            200,
            json={
                "total_count": 7,
                "result_set": [{"identifier": "1GOJ"}, {"identifier": "1BG2"}],
            },
        )
        self.gql = gql if gql is not None else {"data": {"entries": [_OTHER, _ENTRY]}}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "search.rcsb.org":
            return self.search
        if isinstance(self.gql, httpx.Response):
            return self.gql
        return httpx.Response(200, json=self.gql)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.mark.asyncio
async def test_search_sends_the_full_text_query_and_hydrates_every_hit_in_one_call():
    srv = _Server()
    async with srv.client() as c:
        total, recs = await pdb.search(c, "kinesin motor")
    assert total == 7 and [r.id for r in recs] == ["pdb:1GOJ", "pdb:1BG2"]
    search, gql = srv.requests
    assert (search.method, f"{search.url.scheme}://{search.url.host}{search.url.path}") == (
        "GET",
        pdb.SEARCH,
    )
    assert json.loads(search.url.params["json"]) == {
        "query": {
            "type": "terminal",
            "service": "full_text",
            "parameters": {"value": "kinesin motor"},
        },
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": 10}},
    }
    assert search.headers["Accept"] == "application/json"
    assert (gql.method, str(gql.url)) == ("POST", pdb.GRAPHQL)
    assert json.loads(gql.content) == {"query": pdb._GQL.format(ids='"1GOJ","1BG2"')}
    assert gql.headers["Content-Type"] == "application/json"
    assert gql.headers["Accept"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "offset", "rows"), [(3, 20, 3), (500, 0, pdb.MAX_SIZE)])
async def test_search_pages_with_offset_and_capped_rows(size, offset, rows):
    srv = _Server()
    async with srv.client() as c:
        await pdb.search(c, "x", size=size, offset=offset)
    paginate = json.loads(srv.requests[0].url.params["json"])["request_options"]["paginate"]
    assert paginate == {"start": offset, "rows": rows}


@pytest.mark.asyncio
async def test_search_zero_hits_204_skips_hydration():
    srv = _Server(search=httpx.Response(204))
    async with srv.client() as c:
        assert await pdb.search(c, "zzzz") == (0, [])
    assert [r.url.host for r in srv.requests] == ["search.rcsb.org"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"result_set": []},
        {"total_count": 3},
        {"total_count": "3", "result_set": []},
        {"total_count": True, "result_set": []},  # bool is an int subclass
        {"total_count": 3, "result_set": {}},
        {},
    ],
)
async def test_search_200_without_count_and_hits_is_an_outage(body):
    """Zero hits come as 204; a 200 lacking either field was read as (0, [])."""
    srv = _Server(search=httpx.Response(200, json=body))
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await pdb.search(c, "x")
    envelope = _http.UpstreamEnvelopeError(
        f"RCSB PDB search 200 without total_count and result_set: keys {sorted(body)}"
    )
    assert str(err.value) == (
        "[UpstreamUnavailableError] RCSB PDB search returned an unparseable 200 body after 2 "
        f"tries: {envelope!r}"
    )
    assert len(srv.requests) == pdb.MAX_RETRIES


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["search.rcsb.org", "data.rcsb.org"])
async def test_outage_is_retried_twice_and_names_the_call(host):
    srv = _Server()
    down = httpx.Response(503, text="busy")
    if host == "search.rcsb.org":
        srv.search = down
        service = "RCSB PDB search"
    else:
        srv.gql = down
        service = "RCSB PDB graphql"
    async with srv.client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await pdb.search(c, "x")
    assert str(err.value) == (
        f"[UpstreamUnavailableError] {service} exhausted 2 retries (last HTTP 503)"
    )
    assert [r.url.host for r in srv.requests].count(host) == pdb.MAX_RETRIES


@pytest.mark.asyncio
async def test_graphql_errors_are_all_quoted():
    gql = {"errors": [{"message": "first"}, "second"]}
    async with _Server(gql=gql).client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await pdb.resolve(c, "pdb:1BG2")
    assert str(err.value) == "[UpstreamUnavailableError] RCSB PDB GraphQL error: first; second"


@pytest.mark.asyncio
async def test_hydrate_skips_null_and_id_less_entries():
    srv = _Server(gql={"data": {"entries": [None, {"struct": {"title": "?"}}, _ENTRY]}})
    async with srv.client() as c:
        _total, recs = await pdb.search(c, "x")
    assert [r.id for r in recs] == ["pdb:1BG2"]


@pytest.mark.asyncio
async def test_resolve_record_whole():
    async with _Server(gql={"data": {"entries": [_ENTRY]}}).client() as c:
        r = await pdb.resolve(c, "pdb:1BG2")
    assert r.title == "Human kinesin motor domain"
    assert r.last_updated == "1998-10-14T00:00:00Z"
    assert r.subjects == ["X-ray"]
    assert r.links == [
        Link(rel="landing_page", target_id="https://www.rcsb.org/structure/1BG2"),
        Link(rel="described_in", target_id="10.1038/380550a0"),
    ]
    assert r.files == [
        FileEntry(
            name="1BG2.cif",
            url="https://files.rcsb.org/download/1BG2.cif",
            mime="chemical/x-cif",
            source="rcsb",
        ),
        FileEntry(
            name="1BG2.pdb",
            url="https://files.rcsb.org/download/1BG2.pdb",
            mime="chemical/x-pdb",
            source="rcsb",
        ),
    ]


def test_normalize_sparse_entry_has_empty_title_and_no_subjects():
    r = pdb._normalize({"rcsb_id": "9ZZZ"})
    assert (r.title, r.subjects, r.last_updated, r.files) == ("", [], None, [])


@pytest.mark.asyncio
async def test_resolve_normalises_a_padded_lowercase_id():
    srv = _Server(gql={"data": {"entries": [_ENTRY]}})
    async with srv.client() as c:
        r = await pdb.resolve(c, "pdb: 1bg2 ")
    assert r.id == "pdb:1BG2"
    assert json.loads(srv.requests[0].content) == {"query": pdb._GQL.format(ids='"1BG2"')}


@pytest.mark.asyncio
async def test_resolve_not_found_messages():
    async with _Server(gql={"data": {"entries": []}}).client() as c:
        with pytest.raises(NotFoundError) as missing:
            await pdb.resolve(c, "pdb:0xxx")
        with pytest.raises(NotFoundError) as malformed:
            await pdb.resolve(c, "pdb:1B")
    assert str(missing.value) == "[NotFoundError] RCSB PDB has no entry 0XXX"
    assert str(malformed.value) == "[NotFoundError] malformed PDB id 'pdb:1B'"


def test_creators_follow_ordinal_and_skip_nameless_rows():
    entry = {
        "audit_author": [
            {"name": "Third", "pdbx_ordinal": 3},
            None,
            {"pdbx_ordinal": 2},
            {"name": "First", "pdbx_ordinal": 1},
            {"name": "Unnumbered"},
        ]
    }
    # A row with no ordinal sorts as 0, ahead of 1.
    assert pdb._creators(entry) == [
        Creator(name="Unnumbered"),
        Creator(name="First"),
        Creator(name="Third"),
    ]


def test_funding_keeps_the_grant_number():
    entry = {
        "pdbx_audit_support": [
            {"funding_organization": "NIH", "grant_number": "R01GM12345"},
            None,
            {"grant_number": "orphan"},
        ]
    }
    assert pdb._funding(entry) == [FundingRef(funder="NIH", award="R01GM12345")]


def test_taxa_need_an_int_taxid_and_a_name_and_the_first_name_wins():
    entry = {
        "polymer_entities": [
            {
                "rcsb_entity_source_organism": [
                    {"ncbi_taxonomy_id": 9606, "ncbi_scientific_name": "Homo sapiens"},
                    {"ncbi_taxonomy_id": "10090", "ncbi_scientific_name": "Mus musculus"},
                    {"ncbi_taxonomy_id": 562},
                ]
            },
            {
                "rcsb_entity_source_organism": [
                    {"ncbi_taxonomy_id": 9606, "ncbi_scientific_name": "human"},
                ]
            },
        ]
    }
    assert pdb._taxa(entry) == [Taxon(taxid=9606, name="Homo sapiens")]
