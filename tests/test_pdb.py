import os

import httpx
import pytest

from data_aggregator_mcp import pdb
from data_aggregator_mcp.errors import NotFoundError

_SEARCH = {
    "total_count": 1997,
    "result_set": [{"identifier": "1GOJ", "score": 1.0}, {"identifier": "1BG2", "score": 0.99}],
}
_GRAPHQL = {
    "data": {
        "entries": [
            {
                "rcsb_id": "1GOJ",
                "struct": {"title": "Fast kinesin"},
                "rcsb_accession_info": {"initial_release_date": "2001-11-30T00:00:00Z"},
                "rcsb_primary_citation": {
                    "year": 2001,
                    "pdbx_database_id_DOI": "10.1093/emboj/20.22.6213",
                    "pdbx_database_id_PubMed": 11707393,
                },
                "rcsb_entry_info": {"experimental_method": "X-ray"},
            },
            {
                "rcsb_id": "1BG2",
                "struct": {"title": "Human kinesin motor domain"},
                "rcsb_accession_info": {"initial_release_date": "1998-10-14T00:00:00Z"},
                "rcsb_primary_citation": {
                    "year": 1996,
                    "pdbx_database_id_DOI": "10.1038/380550a0",
                    "pdbx_database_id_PubMed": 8606779,
                },
                "rcsb_entry_info": {"experimental_method": "X-ray"},
            },
        ]
    }
}


def _route(request):
    if "search.rcsb.org" in request.url.host:
        return httpx.Response(200, json=_SEARCH)
    return httpx.Response(200, json=_GRAPHQL)


@pytest.mark.asyncio
async def test_search_hydrates_titles_and_doi():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route)) as c:
        total, recs = await pdb.search(c, "kinesin", size=2)
    assert total == 1997
    assert [r.id for r in recs] == ["pdb:1GOJ", "pdb:1BG2"]
    assert recs[1].title == "Human kinesin motor domain"
    assert recs[1].doi == "10.1038/380550a0" and recs[1].year == 1996
    assert recs[1].source == "pdb" and recs[1].kind == "dataset"
    assert recs[1].identifiers.get("pmid") == "8606779"


@pytest.mark.asyncio
async def test_search_empty_returns_zero():
    async def handler(request):
        return httpx.Response(200, json={"total_count": 0, "result_set": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await pdb.search(c, "zzzznohit", size=10) == (0, [])


@pytest.mark.asyncio
async def test_resolve_attaches_structure_files():
    async def handler(request):
        return httpx.Response(200, json=_GRAPHQL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await pdb.resolve(c, "pdb:1BG2")
    assert r.id == "pdb:1BG2" and r.title == "Human kinesin motor domain"
    exts = {f.name.rsplit(".", 1)[-1] for f in r.files}
    assert {"cif", "pdb"} <= exts
    assert all(f.url and f.url.startswith("https://files.rcsb.org/") for f in r.files)
    assert r.identifiers.get("pmid") == "8606779" and r.doi == "10.1038/380550a0"


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    async def handler(request):
        return httpx.Response(200, json={"data": {"entries": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(NotFoundError):
            await pdb.resolve(c, "pdb:0XXX")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"entries": []}}))
    ) as c:
        with pytest.raises(NotFoundError):
            await pdb.resolve(c, "pdb:")


@pytest.mark.asyncio
async def test_resolve_rejects_injection_id_before_network():
    # An id with GraphQL-breaking chars must fail loud on the format guard, never
    # reaching the network (the handler would raise if hit).
    def boom(request):
        raise AssertionError("network must not be touched for a malformed id")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(NotFoundError):
            await pdb.resolve(c, 'pdb:1BG2"]}')


_GRAPHQL_PROVENANCE = {
    "data": {
        "entries": [
            {
                "rcsb_id": "7XYZ",
                "struct": {"title": "Mouse complex"},
                "rcsb_accession_info": {"initial_release_date": "2022-05-04T00:00:00Z"},
                "rcsb_primary_citation": {"year": 2022},
                "rcsb_entry_info": {"experimental_method": "X-ray"},
                "audit_author": [
                    {"name": "Park, S.H.", "pdbx_ordinal": 1},
                    {"name": "Song, H.K.", "pdbx_ordinal": 2},
                ],
                "pdbx_audit_support": [
                    {
                        "funding_organization": "Other government",
                        "grant_number": None,
                        "country": "Korea, Republic Of",
                    }
                ],
                "polymer_entities": [
                    {
                        "rcsb_entity_source_organism": [
                            {"ncbi_taxonomy_id": 10090, "ncbi_scientific_name": "Mus musculus"}
                        ]
                    }
                ],
            }
        ]
    }
}


@pytest.mark.asyncio
async def test_resolve_attaches_provenance():
    """D5: audit_author -> creators (ordered), source organism -> taxa (deduped),
    pdbx_audit_support -> funding (only when an organization is present)."""

    async def handler(request):
        return httpx.Response(200, json=_GRAPHQL_PROVENANCE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await pdb.resolve(c, "pdb:7XYZ")
    assert [cr.name for cr in r.creators] == ["Park, S.H.", "Song, H.K."]
    assert [(t.taxid, t.name) for t in r.taxa] == [(10090, "Mus musculus")]
    assert [(f.funder, f.award) for f in r.funding] == [("Other government", None)]


def test_normalize_provenance_sparse_is_clean():
    """A classic entry with no support record / no source organism stays empty —
    no funding row fabricated from a null pdbx_audit_support."""
    rec = pdb._normalize(_GRAPHQL["data"]["entries"][1])
    assert rec.funding == [] and rec.taxa == [] and rec.creators == []


def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "pdb" in router.available_sources()
    assert router._ADAPTERS["pdb"] is pdb
    assert "pdb:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "pdb" for s in server._SOURCES)


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await pdb.search(c, "kinesin", size=3)
        assert total > 0 and recs and recs[0].id.startswith("pdb:")
        full = await pdb.resolve(c, recs[0].id)
        assert any(f.name.endswith(".cif") for f in full.files)
