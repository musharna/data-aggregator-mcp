from __future__ import annotations

import os
import re
from unittest.mock import AsyncMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import mesh, router, taxonomy
from data_aggregator_mcp.errors import UpstreamUnavailableError, ValidationError
from data_aggregator_mcp.models import DataResource

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _res(id_: str, source: str, doi: str | None) -> DataResource:
    return DataResource(id=id_, source=source, kind="dataset", title="t", doi=doi)


_ZENODO_REC = {
    "id": 123,
    "doi": "10.5281/zenodo.123",
    "metadata": {
        "title": "z",
        "resource_type": {"type": "dataset"},
        "publication_date": "2024-01-01",
    },
    "files": [],
}
_DATACITE_ITEM = {
    "id": "10.5061/dryad.x",
    "attributes": {
        "doi": "10.5061/dryad.x",
        "titles": [{"title": "d"}],
        "publicationYear": 2024,
        "types": {"resourceTypeGeneral": "Dataset"},
    },
    "relationships": {"client": {"data": {"id": "dryad.dryad"}}},
}


def test_available_sources_lists_all_adapters() -> None:
    assert router.available_sources() == [
        "zenodo",
        "dataone",
        "cellxgene",
        "datacite",
        "dandi",
        "omics",
        "literature",
        "huggingface",
        "omicsdi",
        "openml",
        "pdb",
        "gwas",
    ]


def test_available_sources_includes_huggingface() -> None:
    assert "huggingface" in router.available_sources()


@pytest.mark.asyncio
async def test_resolve_routes_hf_prefix(monkeypatch) -> None:
    router._RESOLVE_CACHE.clear()
    called = {}

    async def fake(client, rid):
        called["rid"] = rid
        return DataResource(id=rid, source="huggingface", kind="dataset", title="t")

    monkeypatch.setattr(router.huggingface, "resolve", fake)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        r = await router.resolve(c, "hf:owner/name")
    assert called["rid"] == "hf:owner/name" and r.source == "huggingface"


def test_select_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown source 'bogus'"):
        router._select(["bogus"])


def test_select_subset() -> None:
    assert list(router._select(["datacite"])) == ["datacite"]


def test_dedup_prefers_native_over_datacite_on_doi_collision() -> None:
    native = _res("zenodo:123", "zenodo", "10.5281/zenodo.123")
    via_dc = _res("datacite:10.5281/zenodo.123", "zenodo", "10.5281/zenodo.123")
    out = router._dedup([via_dc, native])
    assert len(out) == 1
    assert out[0].id == "zenodo:123"  # native (fetchable) wins regardless of order


def test_dedup_is_case_insensitive_on_doi() -> None:
    a = _res("zenodo:1", "zenodo", "10.5281/ZENODO.1")
    b = _res("datacite:10.5281/zenodo.1", "zenodo", "10.5281/zenodo.1")
    assert len(router._dedup([a, b])) == 1


def test_dedup_keeps_records_without_doi() -> None:
    a = _res("zenodo:1", "zenodo", None)
    b = _res("zenodo:2", "zenodo", None)
    assert len(router._dedup([a, b])) == 2


async def test_search_fans_out_and_merges(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=rna&size=10",
        json={"hits": {"total": 1, "hits": [_ZENODO_REC]}},
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rna&page%5Bsize%5D=10",
        json={"data": [_DATACITE_ITEM], "meta": {"total": 1}},
    )
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(
            client, "rna", sources=["zenodo", "datacite"]
        )
    assert errors == {}
    ids = {r.id for r in results}
    assert ids == {"zenodo:123", "datacite:10.5061/dryad.x"}
    assert total == 2


async def test_search_captures_per_source_error_without_failing(httpx_mock: HTTPXMock) -> None:
    # zenodo succeeds; datacite 500s past its retries → captured, not raised
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=rna&size=10",
        json={"hits": {"total": 1, "hits": [_ZENODO_REC]}},
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rna&page%5Bsize%5D=10",
        status_code=500,
        is_reusable=True,
    )
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(
            client, "rna", sources=["zenodo", "datacite"]
        )
    assert [r.id for r in results] == ["zenodo:123"]
    assert "datacite" in errors
    assert "UpstreamUnavailableError" in errors["datacite"]


async def test_search_respects_sources_filter(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rna&page%5Bsize%5D=10",
        json={"data": [_DATACITE_ITEM], "meta": {"total": 1}},
    )
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(client, "rna", sources=["datacite"])
    assert [r.id for r in results] == ["datacite:10.5061/dryad.x"]
    assert errors == {}


def test_interleave_gives_each_source_fair_share() -> None:
    # pure unit: a flat concat + truncate would return only the first list.
    zen = [_res(f"zenodo:{i}", "zenodo", f"10.5281/zenodo.{i}") for i in range(5)]
    dc = [_res(f"datacite:10.5061/d.{i}", "dryad", f"10.5061/d.{i}") for i in range(5)]
    merged = router.interleave([zen, dc])
    # round-robin: zenodo:0, datacite:0, zenodo:1, datacite:1, ...
    assert merged[0].id == "zenodo:0"
    assert merged[1].id == "datacite:10.5061/d.0"
    # the top-5 slice must contain BOTH sources (the starvation regression)
    top5 = merged[:5]
    assert any(r.id.startswith("zenodo:") for r in top5)
    assert any(r.id.startswith("datacite:") for r in top5)


async def test_search_does_not_starve_later_source(httpx_mock: HTTPXMock) -> None:
    # both sources return a FULL page of distinct-DOI hits; the merged top-`size`
    # must still include DataCite (regression: flat-concat truncation hid it).
    zen_hits = [
        {
            "id": i,
            "doi": f"10.5281/zenodo.{i}",
            "metadata": {
                "title": f"z{i}",
                "resource_type": {"type": "dataset"},
                "publication_date": "2024-01-01",
            },
            "files": [],
        }
        for i in range(5)
    ]
    dc_hits = [
        {
            "id": f"10.5061/dryad.{i}",
            "attributes": {
                "doi": f"10.5061/dryad.{i}",
                "titles": [{"title": f"d{i}"}],
                "publicationYear": 2024,
                "types": {"resourceTypeGeneral": "Dataset"},
            },
            "relationships": {"client": {"data": {"id": "dryad.dryad"}}},
        }
        for i in range(5)
    ]
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=x&size=5",
        json={"hits": {"total": 5, "hits": zen_hits}},
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=x&page%5Bsize%5D=5",
        json={"data": dc_hits, "meta": {"total": 5}},
    )
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(
            client, "x", size=5, sources=["zenodo", "datacite"]
        )
    assert len(results) == 5
    sources_seen = {r.id.split(":", 1)[0] for r in results}
    assert sources_seen == {"zenodo", "datacite"}  # both represented, neither starved


async def test_resolve_routes_zenodo_prefix(httpx_mock: HTTPXMock) -> None:
    router._RESOLVE_CACHE.clear()
    httpx_mock.add_response(url="https://zenodo.org/api/records/123", json=_ZENODO_REC)
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "zenodo:123")
    assert r.id == "zenodo:123"


async def test_resolve_routes_bare_numeric_to_zenodo(httpx_mock: HTTPXMock) -> None:
    router._RESOLVE_CACHE.clear()
    httpx_mock.add_response(url="https://zenodo.org/api/records/123", json=_ZENODO_REC)
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "123")
    assert r.id == "zenodo:123"


async def test_resolve_routes_datacite_prefix(httpx_mock: HTTPXMock) -> None:
    router._RESOLVE_CACHE.clear()
    httpx_mock.add_response(
        url="https://api.datacite.org/dois/10.5061/dryad.x",
        json={"data": _DATACITE_ITEM},
    )
    # DataCite resolve now fans out to the Dryad manifest resolver; an empty
    # version link short-circuits dryad.files to [] (no /files call).
    httpx_mock.add_response(
        url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.x",
        json={"_links": {}},
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "datacite:10.5061/dryad.x")
    assert r.source == "dryad"


async def test_resolve_routes_bare_doi_to_datacite(httpx_mock: HTTPXMock) -> None:
    router._RESOLVE_CACHE.clear()
    httpx_mock.add_response(
        url="https://api.datacite.org/dois/10.5061/dryad.x",
        json={"data": _DATACITE_ITEM},
    )
    httpx_mock.add_response(
        url="https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.x",
        json={"_links": {}},
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "10.5061/dryad.x")
    assert r.doi == "10.5061/dryad.x"


async def test_resolve_unroutable_id_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="cannot route id"):
            await router.resolve(client, "garbage-no-prefix")


async def test_default_search_includes_omics(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://zenodo.org/api/records?q=rna&size=10",
        json={"hits": {"total": 1, "hits": [_ZENODO_REC]}},
    )
    httpx_mock.add_response(
        url="https://api.datacite.org/dois?query=rna&page%5Bsize%5D=10",
        json={"data": [_DATACITE_ITEM], "meta": {"total": 1}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=rna&retmax=10&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    for db in ("sra", "bioproject"):
        httpx_mock.add_response(
            url=f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db={db}&term=rna&retmax=10&retmode=json",
            json={"esearchresult": {"count": "0", "idlist": []}},
        )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {"accession": "GSE1", "title": "g", "pdat": "2024/01/01"},
            }
        },
    )
    # literature is the 4th default source: pubmed + openaire both return empty here
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=rna&retmax=10&retmode=json",
        json={"esearchresult": {"count": "0", "idlist": []}},
    )
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts?search=rna&type=publication&pageSize=10",
        json={"header": {"numFound": 0}, "results": []},
    )
    # huggingface is the 5th default source: returns empty here
    httpx_mock.add_response(
        url="https://huggingface.co/api/datasets?search=rna&limit=10&full=true",
        json=[],
    )
    # dataone + omicsdi are also default sources: return empty here
    httpx_mock.add_response(
        url=re.compile(r"https://cn\.dataone\.org/cn/v2/query/solr/.*"),
        json={"response": {"numFound": 0, "docs": []}},
    )
    httpx_mock.add_response(
        url=re.compile(r"https://www\.omicsdi\.org/ws/dataset/search.*"),
        json={"datasets": []},
    )
    # dandi is also a default source: returns empty here
    httpx_mock.add_response(
        url=re.compile(r"https://api\.dandiarchive\.org/api/dandisets/.*"),
        json={"count": 0, "results": []},
    )
    # cellxgene is also a default source: empty collections list → no matches
    httpx_mock.add_response(
        url=re.compile(r"https://api\.cellxgene\.cziscience\.com/curation/v1/collections.*"),
        json=[],
    )
    # openml is also a default source: returns empty here
    httpx_mock.add_response(
        url=re.compile(r"https://www\.openml\.org/api/v1/json/data/list/.*"),
        json={"data": {"dataset": []}},
    )
    # pdb is also a default source: empty result_set → no GraphQL hydration call
    httpx_mock.add_response(
        url=re.compile(r"https://search\.rcsb\.org/rcsbsearch/v2/query.*"),
        json={"total_count": 0, "result_set": []},
    )
    # gwas is also a default source (discovery-only): returns empty here
    httpx_mock.add_response(
        url=re.compile(
            r"https://www\.ebi\.ac\.uk/gwas/rest/api/studies/search/findByDiseaseTrait.*"
        ),
        json={"_embedded": {"studies": []}, "page": {"totalElements": 0}},
    )
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(client, "rna")
    assert errors == {}
    ids = {r.id for r in results}
    assert "geo:GSE1" in ids
    assert {"zenodo:123", "datacite:10.5061/dryad.x"} <= ids


async def test_resolve_routes_omics_prefixes(httpx_mock: HTTPXMock, monkeypatch) -> None:
    router._RESOLVE_CACHE.clear()
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE1[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {"accession": "GSE1", "title": "g", "pdat": "2024/01/01"},
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GSE1")
    assert r.id == "geo:GSE1"


async def test_resolve_routes_pubmed_prefix(httpx_mock: HTTPXMock, monkeypatch) -> None:
    router._RESOLVE_CACHE.clear()
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    _EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {
                    "uid": "1",
                    "title": "p",
                    "sortpubdate": "2020/01/01 00:00",
                    "authors": [],
                    "articleids": [],
                },
            }
        },
    )
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db={db}&id=1&retmode=json",
            json={"linksets": [{}]},
        )
    # abstract enrichment via efetch (no AbstractText → description stays None).
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=1&retmode=xml",
        text="<PubmedArticleSet></PubmedArticleSet>",
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "pubmed:1")
    assert r.id == "pubmed:1"


async def test_resolve_routes_openaire_prefix(httpx_mock: HTTPXMock) -> None:
    router._RESOLVE_CACHE.clear()
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts/abc",
        json={
            "id": "abc",
            "mainTitle": "t",
            "pids": [],
            "instances": [],
            "authors": [],
            "subjects": [],
        },
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "openaire:abc")
    assert r.id == "openaire:abc"
    assert r.source == "openaire"


async def test_search_expands_organism_synonyms(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy,
        "resolve_taxon",
        AsyncMock(
            return_value=taxonomy.TaxonInfo(
                taxid=99112,
                canonical_name="Phelipanche aegyptiaca",
                synonyms=("Orobanche aegyptiaca",),
                is_plant=True,
            )
        ),
    )
    captured = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        total, results, errors, expansion = await router.search(
            client, "small RNA", organism="Orobanche aegyptiaca", sources=["zenodo"]
        )
    assert "small RNA" in captured["query"]
    assert "Phelipanche aegyptiaca" in captured["query"]
    assert "Orobanche aegyptiaca" in captured["query"]
    assert expansion is not None
    assert expansion.taxid == 99112
    assert expansion.synonyms == ["Orobanche aegyptiaca"]
    assert errors == {}


async def test_search_organism_lookup_failure_surfaces_error_and_runs_unexpanded(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.setattr(
        taxonomy, "resolve_taxon", AsyncMock(side_effect=UpstreamUnavailableError("NCBI down"))
    )
    captured = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        total, results, errors, expansion = await router.search(
            client, "small RNA", organism="Phelipanche aegyptiaca", sources=["zenodo"]
        )
    assert captured["query"] == "small RNA"  # ran un-expanded
    assert expansion is None
    assert "taxonomy" in errors
    assert "UpstreamUnavailableError" in errors["taxonomy"]


async def test_search_no_organism_param_skips_taxonomy(monkeypatch) -> None:
    called = AsyncMock()
    monkeypatch.setattr(taxonomy, "resolve_taxon", called)

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        total, results, errors, expansion = await router.search(client, "rna", sources=["zenodo"])
    called.assert_not_awaited()
    assert expansion is None


def _mesh_info() -> mesh.MeshInfo:
    return mesh.MeshInfo(
        ui="D001943",
        canonical="Breast Neoplasms",
        synonyms=("Breast Cancer",),
    )


def test_or_group_neutralizes_embedded_quotes_and_drops_empties() -> None:
    # Free-text ontology labels with a stray double-quote must not break the
    # surrounding quoting handed to downstream adapters. _or_group is shared by
    # organism + disease expansion, so this pins the safety once.
    assert router._or_group(["Breast Neoplasms", 'a"b', "   ", '"']) == (
        '"Breast Neoplasms" OR "a b"'
    )


async def test_search_disease_synonym_with_quote_produces_wellformed_query(monkeypatch) -> None:
    info = mesh.MeshInfo(ui="D000001", canonical="Foo", synonyms=('bar"baz',))
    monkeypatch.setattr(mesh, "resolve_mesh", AsyncMock(return_value=info))
    captured: dict[str, str] = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        await router.search_page(client, query="q", disease="foo", sources=["zenodo"])
    # stray quote neutralized → balanced quotes, no injected boolean tokens
    assert captured["query"].count('"') % 2 == 0
    assert "bar baz" in captured["query"]


async def test_search_expands_disease_synonyms(monkeypatch) -> None:
    monkeypatch.setattr(mesh, "resolve_mesh", AsyncMock(return_value=_mesh_info()))
    captured = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="tumor rna", disease="breast cancer", sources=["zenodo"]
        )
    assert "tumor rna" in captured["query"]
    assert "Breast Neoplasms" in captured["query"]
    assert "Breast Cancer" in captured["query"]
    assert result.mesh_expansion is not None
    assert result.mesh_expansion.mesh_ui == "D001943"
    assert result.mesh_expansion.canonical_name == "Breast Neoplasms"
    assert result.mesh_expansion.synonyms == ["Breast Cancer"]
    assert result.errors == {}


async def test_search_disease_and_organism_compose_stacked_and_groups(monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy,
        "resolve_taxon",
        AsyncMock(
            return_value=taxonomy.TaxonInfo(
                taxid=9606,
                canonical_name="Homo sapiens",
                synonyms=("human",),
                is_plant=False,
            )
        ),
    )
    monkeypatch.setattr(mesh, "resolve_mesh", AsyncMock(return_value=_mesh_info()))
    captured = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client,
            query="rna",
            organism="human",
            disease="breast cancer",
            sources=["zenodo"],
        )
    q = captured["query"]
    # disease expands the ALREADY organism-expanded query → two stacked AND-groups
    assert (
        q == '((rna) AND ("Homo sapiens" OR "human")) AND ("Breast Neoplasms" OR "Breast Cancer")'
    )
    assert result.taxon_expansion is not None
    assert result.mesh_expansion is not None


async def test_search_disease_lookup_failure_surfaces_error_and_runs_unexpanded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mesh, "resolve_mesh", AsyncMock(side_effect=UpstreamUnavailableError("NCBI down"))
    )
    captured = {}

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured["query"] = query
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="tumor rna", disease="breast cancer", sources=["zenodo"]
        )
    assert captured["query"] == "tumor rna"  # ran un-expanded
    assert result.mesh_expansion is None
    assert "mesh" in result.errors
    assert "UpstreamUnavailableError" in result.errors["mesh"]


async def test_search_no_disease_param_skips_mesh(monkeypatch) -> None:
    called = AsyncMock()
    monkeypatch.setattr(mesh, "resolve_mesh", called)

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 0, []

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(client, query="rna", sources=["zenodo"])
    called.assert_not_awaited()
    assert result.mesh_expansion is None


async def test_search_disease_round_trips_cursor_without_reexpanding(monkeypatch) -> None:
    resolve = AsyncMock(return_value=_mesh_info())
    monkeypatch.setattr(mesh, "resolve_mesh", resolve)
    captured: list[str] = []

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        captured.append(query)
        # return more than one page so a next_cursor is emitted
        recs = [_res(f"zenodo:{offset}", "zenodo", None)]
        return 5, recs

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        page1 = await router.search_page(
            client, query="tumor", disease="breast cancer", size=1, sources=["zenodo"]
        )
        assert page1.next_cursor is not None
        assert resolve.await_count == 1
        page2 = await router.search_page(client, cursor=page1.next_cursor)
    # continuation must NOT re-expand → resolve_mesh still called only once
    assert resolve.await_count == 1
    # disease is carried on the cursor (so a future change could re-expand) but the
    # echo is frozen to None on continuation, mirroring organism expansion.
    from data_aggregator_mcp import _cursor

    assert _cursor.decode(page1.next_cursor)["disease"] == "breast cancer"
    assert page2.mesh_expansion is None


def _plant_info() -> taxonomy.TaxonInfo:
    return taxonomy.TaxonInfo(
        taxid=99112,
        canonical_name="Phelipanche aegyptiaca",
        synonyms=("Orobanche aegyptiaca",),
        is_plant=True,
    )


async def test_enrich_resource_fills_taxa_and_plant_crosslink(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_plant_info()))
    r = DataResource(
        id="geo:GSE1",
        source="geo",
        kind="study",
        title="t",
        organism=["Phelipanche aegyptiaca"],
    )
    async with httpx.AsyncClient() as client:
        out = await router._enrich_resource(client, r)
    assert [(t.taxid, t.name) for t in out.taxa] == [(99112, "Phelipanche aegyptiaca")]
    assert ("described_in", "plant-genomics:taxid:99112") in [
        (lnk.rel, lnk.target_id) for lnk in out.links
    ]


async def test_enrich_resource_non_plant_has_no_crosslink(monkeypatch) -> None:
    info = taxonomy.TaxonInfo(
        taxid=9606, canonical_name="Homo sapiens", synonyms=(), is_plant=False
    )
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=info))
    r = DataResource(
        id="sra:SRX1",
        source="sra",
        kind="sequencing_run",
        title="t",
        organism=["Homo sapiens"],
    )
    async with httpx.AsyncClient() as client:
        out = await router._enrich_resource(client, r)
    assert [t.taxid for t in out.taxa] == [9606]
    assert out.links == []


async def test_enrich_skips_resource_without_organism(monkeypatch) -> None:
    called = AsyncMock()
    monkeypatch.setattr(taxonomy, "resolve_taxon", called)
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    async with httpx.AsyncClient() as client:
        out = await router._enrich(client, [r], {})
    called.assert_not_awaited()
    assert out[0].taxa == []


async def test_search_enriches_results(monkeypatch) -> None:
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_plant_info()))

    async def fake_omics_search(client, query, *, size=10, offset=0):
        return 1, [
            DataResource(
                id="geo:GSE1",
                source="geo",
                kind="study",
                title="t",
                organism=["Phelipanche aegyptiaca"],
            )
        ]

    monkeypatch.setattr("data_aggregator_mcp.omics.search", fake_omics_search)
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(client, "x", sources=["omics"])
    assert results[0].taxa[0].taxid == 99112
    assert any(lnk.target_id == "plant-genomics:taxid:99112" for lnk in results[0].links)


async def test_search_enrichment_failure_surfaces_taxonomy_error(monkeypatch) -> None:
    monkeypatch.setattr(
        taxonomy, "resolve_taxon", AsyncMock(side_effect=UpstreamUnavailableError("down"))
    )

    async def fake_omics_search(client, query, *, size=10, offset=0):
        return 1, [
            DataResource(
                id="geo:GSE1",
                source="geo",
                kind="study",
                title="t",
                organism=["Phelipanche aegyptiaca"],
            )
        ]

    monkeypatch.setattr("data_aggregator_mcp.omics.search", fake_omics_search)
    async with httpx.AsyncClient() as client:
        total, results, errors, _exp = await router.search(client, "x", sources=["omics"])
    assert "taxonomy" in errors
    assert results[0].taxa == []  # enrichment failed but the result still returned


async def test_enrich_resource_dedups_same_taxid_from_two_names(monkeypatch) -> None:
    # two raw organism strings resolving to the SAME taxid → one Taxon, one link
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_plant_info()))
    r = DataResource(
        id="geo:GSE1",
        source="geo",
        kind="study",
        title="t",
        organism=["Orobanche aegyptiaca", "Phelipanche aegyptiaca"],
    )
    async with httpx.AsyncClient() as client:
        out = await router._enrich_resource(client, r)
    assert len(out.taxa) == 1 and out.taxa[0].taxid == 99112
    assert len([lnk for lnk in out.links if lnk.target_id == "plant-genomics:taxid:99112"]) == 1


async def test_resolve_enriches_with_taxon(httpx_mock: HTTPXMock, monkeypatch) -> None:
    router._RESOLVE_CACHE.clear()
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setattr(taxonomy, "resolve_taxon", AsyncMock(return_value=_plant_info()))
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE1[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {
                    "accession": "GSE1",
                    "title": "g",
                    "pdat": "2024/01/01",
                    "taxon": "Phelipanche aegyptiaca",
                },
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GSE1")
    assert r.taxa[0].taxid == 99112
    assert any(lnk.target_id == "plant-genomics:taxid:99112" for lnk in r.links)


async def test_resolve_survives_taxonomy_failure(httpx_mock: HTTPXMock, monkeypatch) -> None:
    router._RESOLVE_CACHE.clear()
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setattr(
        taxonomy, "resolve_taxon", AsyncMock(side_effect=UpstreamUnavailableError("down"))
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE1[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["1"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {
                    "accession": "GSE1",
                    "title": "g",
                    "pdat": "2024/01/01",
                    "taxon": "Phelipanche aegyptiaca",
                },
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GSE1")
    assert r.id == "geo:GSE1"  # core record returned
    assert r.taxa == []  # enrichment degraded gracefully


async def test_enrich_resource_mixed_plant_and_non_plant(monkeypatch) -> None:
    plant = taxonomy.TaxonInfo(
        taxid=99112,
        canonical_name="Phelipanche aegyptiaca",
        synonyms=(),
        is_plant=True,
    )
    animal = taxonomy.TaxonInfo(
        taxid=9606,
        canonical_name="Homo sapiens",
        synonyms=(),
        is_plant=False,
    )

    async def fake_resolve(client, name):
        return plant if name == "Phelipanche aegyptiaca" else animal

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve)
    r = DataResource(
        id="sra:SRX1",
        source="sra",
        kind="sequencing_run",
        title="t",
        organism=["Phelipanche aegyptiaca", "Homo sapiens"],
    )
    async with httpx.AsyncClient() as client:
        out = await router._enrich_resource(client, r)
    assert {t.taxid for t in out.taxa} == {99112, 9606}  # both normalized
    plant_links = [lnk.target_id for lnk in out.links if lnk.rel == "described_in"]
    assert plant_links == ["plant-genomics:taxid:99112"]  # only the plant gets a cross-link


@_live_only
async def test_live_search_synonym_expansion_fires() -> None:
    taxonomy._CACHE.clear()
    async with httpx.AsyncClient() as client:
        total, results, errors, expansion = await router.search(
            client, "small RNA", organism="Phelipanche aegyptiaca", sources=["literature"]
        )
    assert expansion is not None
    assert expansion.taxid == 99112
    assert expansion.synonyms  # non-empty: at least one synonym was added
    assert "taxonomy" not in errors


@_live_only
async def test_live_pagination_walks_zenodo_datacite() -> None:
    """Real-execution boundary probe: page 2 (via next_cursor) returns records
    disjoint from page 1 against the live Zenodo + DataCite APIs."""
    async with httpx.AsyncClient() as client:
        p1 = await router.search_page(
            client, query="climate", size=5, sources=["zenodo", "datacite"]
        )
        assert p1.next_cursor is not None
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    ids1 = {r.id for r in p1.results}
    ids2 = {r.id for r in p2.results}
    assert ids1 and ids2
    assert ids1.isdisjoint(ids2)  # paging advanced, did not repeat page 1


# --- Task 8: search_page pagination + filters ----------------------------------


def _pres(rid, *, doi=None, year=2020, kind="dataset", source="zenodo"):
    return DataResource(id=rid, source=source, kind=kind, title=rid, doi=doi, year=year)


def _mock_adapter(monkeypatch, name, pages):
    """pages: dict offset -> (total, [DataResource]). search() looks up by offset."""

    async def search(client, query, *, size, offset=0):
        return pages.get(offset, (0, []))

    monkeypatch.setattr(router._ADAPTERS[name], "search", search)


async def test_fresh_search_sets_next_cursor_when_full_window(monkeypatch) -> None:
    _mock_adapter(
        monkeypatch,
        "zenodo",
        {0: (100, [_pres(f"z{i}", doi=f"10.z/{i}") for i in range(10)])},
    )
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        page = await router.search_page(client, query="q", size=10)
    assert page.next_cursor is not None


async def test_continuation_advances_offsets(monkeypatch) -> None:
    _mock_adapter(
        monkeypatch,
        "zenodo",
        {
            0: (100, [_pres(f"z{i}", doi=f"10.z/{i}") for i in range(10)]),
            10: (100, [_pres(f"z{i}", doi=f"10.z/{i}") for i in range(10, 20)]),
        },
    )
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, []), 10: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        p1 = await router.search_page(client, query="q", size=10)
        assert p1.next_cursor is not None
        p2 = await router.search_page(client, cursor=p1.next_cursor)
    ids1 = {r.id for r in p1.results}
    ids2 = {r.id for r in p2.results}
    assert ids1.isdisjoint(ids2)  # page 2 walked deeper


async def test_filter_stall_is_avoided(monkeypatch) -> None:
    # all page-1 records are year=1999 (filtered out by published_after=2010);
    # page-2 records pass. Offsets MUST advance past the rejected page.
    _mock_adapter(
        monkeypatch,
        "zenodo",
        {
            0: (100, [_pres(f"z{i}", doi=f"10.z/{i}", year=1999) for i in range(10)]),
            10: (100, [_pres(f"z{i}", doi=f"10.z/{i}", year=2015) for i in range(10, 20)]),
        },
    )
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, []), 10: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        p1 = await router.search_page(client, query="q", size=10, published_after=2010)
    assert p1.results == [] and p1.next_cursor is not None  # advanced, not stalled


async def test_year_and_kind_filters(monkeypatch) -> None:
    recs = [
        _pres("a", doi="10/a", year=2005, kind="dataset"),
        _pres("b", doi="10/b", year=2018, kind="dataset"),
        _pres("c", doi="10/c", year=2018, kind="publication"),
        _pres("d", doi="10/d", year=None, kind="dataset"),
    ]
    _mock_adapter(monkeypatch, "zenodo", {0: (4, recs)})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        p = await router.search_page(
            client, query="q", size=10, published_after=2010, kind="dataset"
        )
    # 2018 dataset only; c wrong kind, a too old, d year=None dropped
    assert {r.id for r in p.results} == {"b"}


async def test_unknown_kind_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        with pytest.raises(ValidationError):
            await router.search_page(client, query="q", kind="nonsense")


async def test_corrupt_cursor_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        with pytest.raises(ValidationError):
            await router.search_page(client, cursor="garbage!!")


async def test_neither_query_nor_cursor_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        with pytest.raises(ValidationError):
            await router.search_page(client, size=10)


async def test_page1_unfiltered_matches_legacy(monkeypatch) -> None:
    recs = [_pres(f"z{i}", doi=f"10.z/{i}") for i in range(5)]
    _mock_adapter(monkeypatch, "zenodo", {0: (5, recs)})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        legacy = await router.search(client, "q", size=10)  # old 4-tuple
        page = await router.search_page(client, query="q", size=10)
    assert [r.id for r in legacy[1]] == [r.id for r in page.results]


async def test_more_uses_upstream_total_not_window_length(monkeypatch) -> None:
    """Regression: a paged adapter returns < size records (page-boundary slice)
    yet still has rows past its offset. ``more`` must key off the upstream total,
    not ``len(recs) == size`` — else pagination stops prematurely and loses the
    deeper results.
    """
    # zenodo reports 100 total hits but this window yields only 6 records
    # (as the offset-slice would produce mid-stream), and all are consumed.
    _mock_adapter(
        monkeypatch,
        "zenodo",
        {0: (100, [_pres(f"z{i}", doi=f"10.z/{i}") for i in range(6)])},
    )
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        page = await router.search_page(client, query="q", size=10)
    assert len(page.results) == 6
    assert page.next_cursor is not None  # 6 consumed < 100 total → more remains


async def test_empty_page_with_nonzero_total_does_not_loop(monkeypatch) -> None:
    """Regression (review M1): an adapter reporting total>0 but returning no
    records must NOT emit a next_cursor — offsets cannot advance on an empty
    page, so a cursor would replay the same window forever.
    """
    _mock_adapter(monkeypatch, "zenodo", {0: (50, [])})
    for n in ("datacite", "omics", "literature"):
        _mock_adapter(monkeypatch, n, {0: (0, [])})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        page = await router.search_page(client, query="q", size=10)
    assert page.results == []
    assert page.next_cursor is None  # no candidates fetched → terminate, don't loop


@_live_only
async def test_live_orcid_funding_relations_extraction() -> None:
    """Real-execution boundary probe: against the live Zenodo + DataCite APIs,
    confirm the new metadata extraction (creator ORCID, funding, related links)
    fires on real response shapes — not just synthetic fixtures.
    """
    async with httpx.AsyncClient() as client:
        # Zenodo authors commonly carry ORCID iDs.
        _t, zen, _e, _x = await router.search(client, "genomics", size=25, sources=["zenodo"])
        # DataCite records commonly carry relatedIdentifiers and/or fundingReferences.
        _t, dc, _e2, _x2 = await router.search(client, "climate", size=25, sources=["datacite"])
    # creators are always structured Creator objects now (well-typed live).
    assert all(hasattr(c, "name") for r in zen + dc for c in r.creators)
    assert any(c.orcid for r in zen for c in r.creators), "no live Zenodo creator ORCID found"
    assert any(r.links for r in dc) or any(r.funding for r in dc), (
        "no live DataCite related-links or funding found"
    )


@pytest.mark.asyncio
async def test_resolve_is_cached_by_id(monkeypatch):
    router._RESOLVE_CACHE.clear()
    calls = {"n": 0}

    async def fake_zenodo_resolve(client, rid):
        calls["n"] += 1
        return DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="X")

    monkeypatch.setattr(router.zenodo, "resolve", fake_zenodo_resolve)
    first = await router.resolve(None, "zenodo:1")
    second = await router.resolve(None, "zenodo:1")
    assert calls["n"] == 1  # second served from cache
    assert first is second


@pytest.mark.asyncio
async def test_search_semantic_reorders_window(monkeypatch):
    async def fake_zen_search(client, query, *, size, offset=0):
        return 2, [
            DataResource(id="zenodo:a", source="zenodo", kind="dataset", title="apple"),
            DataResource(id="zenodo:b", source="zenodo", kind="dataset", title="banana"),
        ]

    monkeypatch.setattr(router.zenodo, "search", fake_zen_search)

    async def fake_rerank(client, query, resources):
        return list(reversed(resources)), None

    monkeypatch.setattr(router.embeddings, "rerank", fake_rerank)

    r = await router.search_page(None, query="fruit", size=10, sources=["zenodo"], rank="semantic")
    assert [x.id for x in r.results] == ["zenodo:b", "zenodo:a"]


@pytest.mark.asyncio
async def test_search_semantic_failsoft_keeps_order_and_notes_error(monkeypatch):
    async def fake_zen_search(client, query, *, size, offset=0):
        return 1, [DataResource(id="zenodo:a", source="zenodo", kind="dataset", title="apple")]

    monkeypatch.setattr(router.zenodo, "search", fake_zen_search)

    async def fake_rerank(client, query, resources):
        return resources, "unavailable"

    monkeypatch.setattr(router.embeddings, "rerank", fake_rerank)

    r = await router.search_page(None, query="x", size=10, sources=["zenodo"], rank="semantic")
    assert [x.id for x in r.results] == ["zenodo:a"]
    assert r.errors.get("semantic") == "unavailable"


async def test_resolve_sets_version_status(monkeypatch) -> None:
    import httpx

    from data_aggregator_mcp import router
    from data_aggregator_mcp.models import DataResource, Link

    async def fake_zenodo_resolve(client, rid):
        return DataResource(
            id="zenodo:1",
            source="zenodo",
            kind="dataset",
            title="t",
            links=[Link(rel="is_previous_version_of", target_id="zenodo:2")],
        )

    monkeypatch.setattr("data_aggregator_mcp.zenodo.resolve", fake_zenodo_resolve)
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "zenodo:1")
    assert r.is_latest is False
    assert r.superseded_by == "zenodo:2"


def test_dataone_and_omicsdi_registered_in_precedence_order():
    names = list(router._ADAPTERS)
    assert "dataone" in names and "omicsdi" in names
    # dataone before datacite (keep the verified copy on a DOI tie); omicsdi after
    # the DOI-bearing backends; openml + pdb + gwas registered last.
    assert names.index("dataone") < names.index("datacite")
    assert names.index("datacite") < names.index("omicsdi")
    assert names[-1] == "gwas"


@pytest.mark.asyncio
async def test_resolve_routes_dataone_prefix(monkeypatch):
    called = {}

    async def fake(client, rid):
        called["rid"] = rid
        from data_aggregator_mcp.models import DataResource

        return DataResource(id=rid, source="dataone", kind="dataset", title="t")

    monkeypatch.setattr("data_aggregator_mcp.dataone.resolve", fake)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "dataone:doi:10.5/x")
    assert called["rid"] == "dataone:doi:10.5/x" and r.source == "dataone"


@pytest.mark.asyncio
async def test_resolve_routes_omicsdi_prefix(monkeypatch):
    async def fake(client, rid):
        from data_aggregator_mcp.models import DataResource

        return DataResource(id=rid, source="omicsdi", kind="study", title="t")

    monkeypatch.setattr("data_aggregator_mcp.omicsdi.resolve", fake)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "omicsdi:pride:PXD000001")
    assert r.source == "omicsdi"


@pytest.mark.asyncio
async def test_resolve_sets_access_modes(monkeypatch) -> None:
    from data_aggregator_mcp import operate
    from data_aggregator_mcp.models import FileEntry

    res = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        files=[FileEntry(name="d.parquet", url="https://h/d.parquet")],
    )

    async def fake_zenodo_resolve(client, rid):
        return res

    monkeypatch.setattr(router.zenodo, "resolve", fake_zenodo_resolve)
    monkeypatch.setattr(operate, "OPERATE_AVAILABLE", True)
    async with httpx.AsyncClient() as c:
        out = await router.resolve(c, "zenodo:1")
    assert "sql" in out.access_modes and "fetch" in out.access_modes
