import os

import httpx
import pytest

from data_aggregator_mcp import pdb
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Link

_SEARCH = {
    "total_count": 1997,
    "result_set": [{"identifier": "1GOJ", "score": 1.0}, {"identifier": "1BG2", "score": 0.99}],
}
_GRAPHQL = {
    "data": {
        "entries": [
            {
                "rcsb_id": "1GOJ",
                "pdbx_database_status": {"pdb_format_compatible": "Y"},
                "struct": {"title": "Fast kinesin"},
                "rcsb_accession_info": {"initial_release_date": "2001-11-30T00:00:00Z"},
                "rcsb_primary_citation": {
                    "year": 2001,
                    "pdbx_database_id_DOI": "10.1093/emboj/20.22.6213",
                    "pdbx_database_id_PubMed": 11707393,
                },
                "rcsb_entry_info": {"experimental_method": "X-ray"},
                "database_2": [
                    {"database_id": "PDB", "pdbx_DOI": "10.2210/pdb1goj/pdb"},
                    {"database_id": "WWPDB", "pdbx_DOI": None},
                ],
            },
            {
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
                "database_2": [
                    {"database_id": "PDB", "pdbx_DOI": "10.2210/pdb1bg2/pdb"},
                    {"database_id": "WWPDB", "pdbx_DOI": None},
                ],
            },
        ]
    }
}


def _route(request):
    if request.url.host == "search.rcsb.org":
        return httpx.Response(200, json=_SEARCH)
    return httpx.Response(200, json=_GRAPHQL)


@pytest.mark.asyncio
async def test_search_hydrates_titles_and_doi():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route)) as c:
        total, recs = await pdb.search(c, "kinesin", size=2)
    assert total == 1997
    assert [r.id for r in recs] == ["pdb:1GOJ", "pdb:1BG2"]
    assert recs[1].title == "Human kinesin motor domain"
    assert recs[1].doi == "10.2210/pdb1bg2/pdb" and recs[1].year == 1996  # the entry's own DOI
    assert Link(rel="described_in", target_id="10.1038/380550a0") in recs[1].links
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
    assert r.identifiers.get("pmid") == "8606779" and r.doi == "10.2210/pdb1bg2/pdb"


@pytest.mark.asyncio
async def test_resolve_lists_legacy_pdb_file_only_when_rcsb_produces_one():
    """B-M4 (audit 2026-09-27): resolve always listed ``<id>.pdb``, but wwPDB makes no
    legacy PDB-format file for entries too large for it (4V6X: 237,685 atoms,
    ``pdb_format_compatible = "N"``). That URL 404s, so ``fetch`` of 4V6X failed as a
    whole, valid .cif included. List the .pdb only when RCSB says it exists."""
    big = {
        "data": {
            "entries": [
                {
                    "rcsb_id": "4V6X",
                    "pdbx_database_status": {"pdb_format_compatible": "N"},
                    "struct": {"title": "Human 80S ribosome"},
                    "database_2": [{"database_id": "PDB", "pdbx_DOI": "10.2210/pdb4v6x/pdb"}],
                }
            ]
        }
    }

    def handler(request):
        return httpx.Response(200, json=big if b"4V6X" in request.content else _GRAPHQL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        large = await pdb.resolve(c, "pdb:4V6X")
        small = await pdb.resolve(c, "pdb:1BG2")  # positive control: both formats exist
    assert [f.name for f in large.files] == ["4V6X.cif"]
    assert [f.name for f in small.files] == ["1BG2.cif", "1BG2.pdb"]


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


def _entry(rid: str, entry_doi: str | None, paper_doi: str) -> dict:
    """Live shape of 6VYB/6VXX (2026-09-27): distinct entry DOIs, one shared paper."""
    return {
        "rcsb_id": rid,
        "struct": {"title": f"SARS-CoV-2 spike {rid}"},
        "rcsb_primary_citation": {
            "year": 2020,
            "pdbx_database_id_DOI": paper_doi,
            "pdbx_database_id_PubMed": 32155444,
        },
        "database_2": [
            {"database_id": "PDB", "pdbx_DOI": entry_doi},
            {"database_id": "EMDB", "pdbx_DOI": None},
        ],
    }


def test_sibling_structures_from_one_paper_survive_doi_dedup():
    """B-H4: doi was the primary-citation (paper) DOI, so dedup_by_doi folded every
    structure published in one paper into one (search kept 14 of 20). doi must be the
    entry's own DOI; the paper DOI stays reachable as a described_in link."""
    from data_aggregator_mcp import relate
    from data_aggregator_mcp._mirror import dedup_by_doi
    from data_aggregator_mcp.models import DataResource

    paper = "10.1016/j.cell.2020.02.058"
    a = pdb._normalize(_entry("6VYB", "10.2210/pdb6vyb/pdb", paper))
    b = pdb._normalize(_entry("6VXX", "10.2210/pdb6vxx/pdb", paper))
    assert [r.id for r in dedup_by_doi([a, b])] == ["pdb:6VYB", "pdb:6VXX"]
    # positive control: the same entry seen twice still collapses on its own DOI
    assert [r.id for r in dedup_by_doi([a, a.model_copy()])] == ["pdb:6VYB"]
    # the paper bridge survives: explicit described_in link to the paper record
    paper_rec = DataResource(
        id="pubmed:32155444", source="pubmed", kind="publication", title="Walls", doi=paper
    )
    kinds = {(h.kind, tuple(h.resources)) for h in relate.detect([a, paper_rec])}
    assert ("explicit_link", ("pdb:6VYB", "pubmed:32155444")) in kinds
    # an entry with no registered PDB DOI keeps doi=None (never a constructed guess)
    assert pdb._normalize(_entry("9ZZZ", None, paper)).doi is None


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


@pytest.mark.asyncio
async def test_search_204_zero_hits_is_empty_not_outage(monkeypatch):
    """Audit 2026-09-22 M14: RCSB's real zero-hit answer is ``204 No Content`` (the
    200-with-empty-result_set fixture above never happens live). It was reported as an
    outage. A 404 on the SEARCH endpoint means the endpoint moved: that must raise,
    not read as "no hits" (H3)."""
    from data_aggregator_mcp import _http
    from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)

    def handler(request):
        q = request.url.params.get("json", "")
        if request.url.host == "search.rcsb.org":
            if "zzzznohit" in q:
                return httpx.Response(204)
            if "gone" in q:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json=_SEARCH)
        return httpx.Response(200, json=_GRAPHQL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await pdb.search(c, "zzzznohit", size=10) == (0, [])
        with pytest.raises((NotFoundError, UpstreamUnavailableError)):
            await pdb.search(c, "gone", size=10)
        total, recs = await pdb.search(c, "kinesin", size=2)  # positive control
    assert total == 1997 and [r.id for r in recs] == ["pdb:1GOJ", "pdb:1BG2"]


@_live_only
@pytest.mark.asyncio
async def test_live_sibling_entries_have_own_dois():
    """B-H4 live: 6VYB and 6VXX share the Walls 2020 paper but are distinct entries."""
    async with httpx.AsyncClient(timeout=60) as c:
        a = await pdb.resolve(c, "pdb:6VYB")
        b = await pdb.resolve(c, "pdb:6VXX")
    assert a.doi == "10.2210/pdb6vyb/pdb" and b.doi == "10.2210/pdb6vxx/pdb"
    paper = Link(rel="described_in", target_id="10.1016/j.cell.2020.02.058")
    assert paper in a.links and paper in b.links


@_live_only
@pytest.mark.asyncio
async def test_live_every_listed_structure_file_exists():
    """B-M4 live: 4V6X has no legacy .pdb (404 upstream); 1BG2 has both formats. Every
    URL resolve lists must answer 200."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        large = await pdb.resolve(c, "pdb:4V6X")
        small = await pdb.resolve(c, "pdb:1BG2")
        statuses = {f.name: (await c.head(f.url)).status_code for f in large.files + small.files}
    assert statuses == {"4V6X.cif": 200, "1BG2.cif": 200, "1BG2.pdb": 200}
