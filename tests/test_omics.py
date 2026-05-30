from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import omics
from data_aggregator_mcp.errors import NotFoundError


def _geo_doc() -> dict:
    return {
        "accession": "GSE332789",
        "title": "Arabidopsis PP2-A5 defense",
        "summary": "TIR-Lectin two-domain protein.",
        "taxon": "Arabidopsis thaliana",
        "gse": "332789",
        "pdat": "2026/05/26",
        "gdstype": "Expression profiling by high throughput sequencing",
        "bioproject": "PRJNA111",
    }


def test_normalize_geo_maps_core_fields() -> None:
    r = omics._normalize_geo(_geo_doc())
    assert r.id == "geo:GSE332789"
    assert r.source == "geo"
    assert r.kind == "study"
    assert r.title == "Arabidopsis PP2-A5 defense"
    assert r.description == "TIR-Lectin two-domain protein."
    assert r.organism == ["Arabidopsis thaliana"]
    assert r.year == 2026
    assert r.accessions == ["GSE332789"]
    assert r.files == []


_EXPXML = (
    "<Summary><Title>Control_2</Title></Summary>"
    '<Experiment acc="SRX33614706" name="Control_2"/>'
    '<Study acc="SRP703893" name="RNA-seq of Arabidopsis thaliana Treated with PSP3"/>'
    '<Organism taxid="3702" ScientificName="Arabidopsis thaliana"/>'
    "<Bioproject>PRJNA1468572</Bioproject>"
)
_RUNS = '<Run acc="SRR38843714" total_spots="1"/>'


def _sra_doc() -> dict:
    return {"expxml": _EXPXML, "runs": _RUNS, "createdate": "2026/05/01"}


def test_normalize_sra_parses_expxml_and_runs() -> None:
    r = omics._normalize_sra(_sra_doc())
    assert r.id == "sra:SRX33614706"
    assert r.source == "sra"
    assert r.kind == "sequencing_run"
    # Study name is the meaningful title; experiment "Control_2" is the fallback
    assert r.title == "RNA-seq of Arabidopsis thaliana Treated with PSP3"
    # the distinct experiment-level Summary/Title is preserved as description
    assert r.description == "Control_2"
    assert r.organism == ["Arabidopsis thaliana"]
    assert "SRX33614706" in r.accessions
    assert "SRR38843714" in r.accessions
    assert "SRP703893" in r.accessions
    assert "PRJNA1468572" in r.accessions
    assert r.files == []  # ENA manifest is attached at resolve, not normalize


def _bp_doc() -> dict:
    return {
        "project_acc": "PRJNA1468572",
        "project_title": "Arabidopsis transcriptome",
        "project_description": "Transcriptome of A. thaliana under stress.",
        "organism_name": "Arabidopsis thaliana",
        "registration_date": "2026/04/01",
    }


def test_normalize_bioproject_maps_core_fields() -> None:
    r = omics._normalize_bioproject(_bp_doc())
    assert r.id == "bioproject:PRJNA1468572"
    assert r.source == "bioproject"
    assert r.kind == "study"
    assert r.title == "Arabidopsis transcriptome"
    assert r.description == "Transcriptome of A. thaliana under stress."
    assert r.organism == ["Arabidopsis thaliana"]
    assert r.accessions == ["PRJNA1468572"]
    assert r.year == 2026
    assert r.files == []


async def test_search_fans_out_across_three_dbs(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    # esearch for each db (one id each)
    for db, uid in [("gds", "1"), ("sra", "2"), ("bioproject", "3")]:
        httpx_mock.add_response(
            url=f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db={db}&term=arabidopsis&retmax=10&retmode=json",
            json={"esearchresult": {"count": "1", "idlist": [uid]}},
        )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=1&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["1"],
                "1": {
                    "accession": "GSE1",
                    "title": "g",
                    "taxon": "Arabidopsis thaliana",
                    "pdat": "2024/01/01",
                },
            }
        },
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=sra&id=2&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["2"],
                "2": {
                    "expxml": '<Experiment acc="SRX9"/><Organism ScientificName="Arabidopsis thaliana"/>',
                    "runs": "",
                    "createdate": "2024/01/01",
                },
            }
        },
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=bioproject&id=3&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["3"],
                "3": {
                    "project_acc": "PRJNA9",
                    "project_title": "b",
                    "organism_name": "Arabidopsis thaliana",
                    "registration_date": "2024/01/01",
                },
            }
        },
    )
    async with httpx.AsyncClient() as client:
        total, results = await omics.search(client, "arabidopsis")
    assert total == 3
    ids = {r.id for r in results}
    assert ids == {"geo:GSE1", "sra:SRX9", "bioproject:PRJNA9"}


async def test_resolve_geo_by_accession(httpx_mock: HTTPXMock, monkeypatch) -> None:
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
        r = await omics.resolve(client, "geo:GSE1")
    assert r.id == "geo:GSE1"
    assert r.files == []  # GEO discovery-only in Phase 3


async def test_resolve_sra_attaches_ena_manifest(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=sra&term=SRX9[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["2"]}},
    )
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=sra&id=2&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["2"],
                "2": {
                    "expxml": '<Experiment acc="SRX9"/>',
                    "runs": '<Run acc="SRR9"/>',
                    "createdate": "2024/01/01",
                },
            }
        },
    )
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/ena/portal/api/filereport?accession=SRX9&result=read_run&fields=run_accession,experiment_accession,study_accession,sample_accession,scientific_name,fastq_ftp,fastq_bytes,fastq_md5&format=json",
        json=[
            {
                "run_accession": "SRR9",
                "fastq_ftp": "ftp.sra.ebi.ac.uk/x/SRR9.fastq.gz",
                "fastq_bytes": "10",
                "fastq_md5": "abc",
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        r = await omics.resolve(client, "sra:SRX9")
    assert r.id == "sra:SRX9"
    assert len(r.files) == 1
    assert r.files[0].url == "https://ftp.sra.ebi.ac.uk/x/SRR9.fastq.gz"


async def test_resolve_unknown_accession_raises(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE404[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "0", "idlist": []}},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no omics record"):
            await omics.resolve(client, "geo:GSE404")


async def test_resolve_unroutable_prefix_raises() -> None:
    # an id whose prefix isn't an omics db must fail loud (no HTTP call made)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="unroutable omics id"):
            await omics.resolve(client, "zenodo:123")


async def test_resolve_bioproject_attaches_sra_links(httpx_mock: HTTPXMock, monkeypatch) -> None:
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    eut = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    # 1) esearch bioproject by accession
    httpx_mock.add_response(
        url=f"{eut}/esearch.fcgi?db=bioproject&term=PRJNA1[ACCN]&retmax=1&retmode=json",
        json={"esearchresult": {"count": "1", "idlist": ["111"]}},
    )
    # 2) esummary bioproject
    httpx_mock.add_response(
        url=f"{eut}/esummary.fcgi?db=bioproject&id=111&version=2.0&retmode=json",
        json={"result": {"uids": ["111"], "111": {"project_acc": "PRJNA1", "project_title": "P"}}},
    )
    # 3) elink bioproject -> sra
    httpx_mock.add_response(
        url=f"{eut}/elink.fcgi?dbfrom=bioproject&db=sra&id=111&retmode=json",
        json={
            "linksets": [
                {"linksetdbs": [{"dbto": "sra", "linkname": "bioproject_sra", "links": ["222"]}]}
            ]
        },
    )
    # 4) esummary sra for the linked run
    httpx_mock.add_response(
        url=f"{eut}/esummary.fcgi?db=sra&id=222&version=2.0&retmode=json",
        json={
            "result": {
                "uids": ["222"],
                "222": {
                    "expxml": '<Experiment acc="SRX9"/><Summary><Title>r</Title></Summary>',
                    "runs": "",
                },
            }
        },
    )
    async with httpx.AsyncClient() as client:
        r = await omics.resolve(client, "bioproject:PRJNA1")
    assert r.files == []
    assert any(lnk.target_id == "sra:SRX9" and lnk.rel == "has_data" for lnk in r.links)


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@live_only
async def test_live_omics_search_spans_dbs() -> None:
    async with httpx.AsyncClient() as client:
        total, results = await omics.search(client, "arabidopsis RNA-seq", size=6)
    assert total >= 1
    assert {r.source for r in results} & {"geo", "sra", "bioproject"}
    assert all(r.id.split(":", 1)[0] in ("geo", "sra", "bioproject") for r in results)


@live_only
async def test_live_resolve_sra_has_ena_files() -> None:
    # SRX079566 is a stably ENA-mirrored experiment (runs SRR292241, SRR390728)
    async with httpx.AsyncClient() as client:
        r = await omics.resolve(client, "sra:SRX079566")
    assert r.id == "sra:SRX079566"
    assert r.files and all(f.url.startswith("https://") for f in r.files)
