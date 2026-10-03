"""What the omics adapter makes of NCBI's answers, beside the live shapes they came from.

Probed live 2026-10-02 (esummary version 2.0): 600 gds summaries (GSE/GSM/GPL/GDS), 400 sra
and 400 bioproject summaries from varied queries. Every field the adapter reads was a
string in all 1,400, and 11 of the 600 gds ``taxon`` values named several organisms.
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import omics, router, taxonomy
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.taxonomy import TaxonInfo

live_only = pytest.mark.skipif(
    os.environ.get("DATA_AGGREGATOR_MCP_LIVE") != "1",
    reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run",
)

# Verbatim live gds taxon values (GSE347972, GDS5814, GSE294673).
_MULTI_TAXON = {
    "Homo sapiens; Mus musculus": ["Homo sapiens", "Mus musculus"],
    "Schizosaccharomyces pombe; Saccharomyces cerevisiae": [
        "Schizosaccharomyces pombe",
        "Saccharomyces cerevisiae",
    ],
    "Homo sapiens; Drosophila melanogaster; Mus musculus": [
        "Homo sapiens",
        "Drosophila melanogaster",
        "Mus musculus",
    ],
}


def test_a_geo_entry_of_several_organisms_lists_each_one() -> None:
    """GEO joins an entry's organisms with "; ". Read as one name, "Homo sapiens; Mus
    musculus" was one organism, and the taxonomy lookup (which NCBI answers with the one
    phrase it can match, here Mus musculus) dropped the other. Positive control: a
    single-organism entry and an entry with no taxon read as before."""
    for taxon, names in _MULTI_TAXON.items():
        r = omics._normalize_geo({"accession": "GSE1", "title": "t", "taxon": taxon})
        assert r.organism == names, taxon
    one = omics._normalize_geo({"accession": "GSE2", "title": "t", "taxon": "Arabidopsis thaliana"})
    assert one.organism == ["Arabidopsis thaliana"]
    assert omics._normalize_geo({"accession": "GSE3", "title": "t", "taxon": ""}).organism == []


async def test_resolve_looks_up_every_organism_of_a_geo_entry(monkeypatch) -> None:
    """End to end through the router: each organism of a two-organism series reaches the
    taxonomy lookup and becomes a taxon of the record."""
    asked: list[str] = []
    known = {"Homo sapiens": 9606, "Mus musculus": 10090}

    async def fake_resolve_taxon(client, name):
        asked.append(name)
        taxid = known.get(name)
        return None if taxid is None else TaxonInfo(taxid, name, (), False)

    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        return 1, ["200347972"]

    async def fake_esummary(client, db, ids):
        doc = {"uid": "200347972", "accession": "GSE347972", "title": "t"}
        return [{**doc, "taxon": "Homo sapiens; Mus musculus", "ftplink": ""}]

    async def no_files(client, ftplink):
        return []

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve_taxon)
    monkeypatch.setattr(omics._eutils, "esearch", fake_esearch)
    monkeypatch.setattr(omics._eutils, "esummary", fake_esummary)
    monkeypatch.setattr(omics.geo, "supplementary_files", no_files)
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GSE347972")
    router._RESOLVE_CACHE.clear()
    assert asked == ["Homo sapiens", "Mus musculus"]
    assert [(t.taxid, t.name) for t in r.taxa] == [(9606, "Homo sapiens"), (10090, "Mus musculus")]


@live_only
async def test_live_a_two_organism_geo_dataset_resolves_with_both_taxa() -> None:
    """GDS5814 (a frozen GEO DataSet) is fission and budding yeast; resolved through the
    router, both organisms are listed and both become taxa."""
    router._RESOLVE_CACHE.clear()
    async with httpx.AsyncClient() as client:
        r = await router.resolve(client, "geo:GDS5814")
    router._RESOLVE_CACHE.clear()
    assert r.organism == ["Schizosaccharomyces pombe", "Saccharomyces cerevisiae"]
    assert {t.taxid for t in r.taxa} == {4896, 4932}


# Live summaries (esummary version 2.0, 2026-10-02), trimmed to the fields the adapter reads.
_GDS = {
    "uid": "6063",
    "accession": "GDS6063",
    "title": "Influenza A effect on plasmacytoid dendritic cells",
    "summary": "Analysis of primary plasmacytoid dendritic cells (pDC) exposed to influenza A.",
    "taxon": "Homo sapiens",
    "pdat": "2016/02/01",
    "ftplink": "ftp://ftp.ncbi.nlm.nih.gov/geo/datasets/GDS6nnn/GDS6063/",
}
_SRA = {
    "uid": "47143239",
    "expxml": (
        "  <Summary><Title>16S rRNA gene amplicon sequencing of Houttuynia cordata"
        " rhizosphere: Broth control replicate 5</Title></Summary>"
        '<Experiment acc="SRX35511800" ver="1" status="public" name="Broth control 5"/>'
        '<Study acc="SRP741891" name="Rhizosphere microbiome in cultivated Houttuynia cordata"/>'
        '<Organism taxid="939928" ScientificName="rhizosphere metagenome"/>'
        "<Bioproject>PRJNA1538143</Bioproject><Biosample>SAMN63810834</Biosample>  "
    ),
    "runs": '    <Run acc="SRR40983380" total_spots="65401" is_public="true"/>    ',
    "createdate": "2026/10/02",
}
_BIOPROJECT = {
    "uid": "1537786",
    "project_acc": "PRJNA1537786",
    "project_title": "Targeted rewiring of the rhizosphere metabolite-microbe axis",
    "project_description": "This study investigates the mechanism by which exogenous ...",
    "organism_name": "",
    "registration_date": "2026/10/01 00:00",
}
_LIVE = {"gds": _GDS, "sra": _SRA, "bioproject": _BIOPROJECT}
_WRONG_TYPES = (None, 7, 7.5, True, [], ["x"], {}, {"x": "y"})


_PREFIX = {db: prefix for prefix, db in omics._DB.items()}


def _answering(monkeypatch, docs: list[dict]) -> None:
    """NCBI stub: esearch finds one uid, esummary answers ``docs[-1]``."""

    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        return 1, ["1"]

    async def fake_esummary(client, db, ids):
        return [docs[-1]]

    monkeypatch.setattr(omics._eutils, "esearch", fake_esearch)
    monkeypatch.setattr(omics._eutils, "esummary", fake_esummary)


async def _search(monkeypatch, db: str, doc: dict):
    """The record a search of ``db`` makes of an esummary answer holding ``doc``."""
    _answering(monkeypatch, [doc])
    _total, recs = await omics.search_subsource(None, _PREFIX[db], "q")
    return recs[0]


def _unreadable(db: str, uid: str, why: str) -> str:
    return (
        f"^\\[UpstreamUnavailableError\\] NCBI esummary \\({db}\\) answered an unreadable "
        f"summary for uid '{uid}': {why}"
    )


async def test_no_wrong_typed_summary_field_escapes_as_a_bare_error(monkeypatch) -> None:
    """Every field of a live summary, given each other JSON type, is refused as
    ``UpstreamUnavailableError`` naming the db, uid and field. On the old code a wrong
    type escaped as a bare ``TypeError``/pydantic error or was read as if it were text.
    Positive control: each live summary reads."""
    assert (await _search(monkeypatch, "gds", _GDS)).id == "geo:GDS6063"
    assert (await _search(monkeypatch, "sra", _SRA)).id == "sra:SRX35511800"
    assert (await _search(monkeypatch, "bioproject", _BIOPROJECT)).id == "bioproject:PRJNA1537786"
    walked = 0
    for db, live in _LIVE.items():
        for key in live:
            if key in ("uid", "ftplink"):
                continue  # read by resolve only; see the resolve test below
            for wrong in _WRONG_TYPES:
                why = f"{key} is {type(wrong).__name__}, not text$"
                with pytest.raises(
                    UpstreamUnavailableError, match=_unreadable(db, live["uid"], why)
                ):
                    await _search(monkeypatch, db, {**live, key: wrong})
                walked += 1
    assert walked == 8 * (5 + 3 + 5)


async def test_a_summary_without_its_accession_is_refused_not_listed_as_an_empty_id(
    monkeypatch,
) -> None:
    """Without its accession a summary became the record ``geo:`` or ``bioproject:``
    (an id nothing can resolve). Positive control: any other text field may be absent
    and reads as empty."""
    for db, key in (("gds", "accession"), ("bioproject", "project_acc")):
        live = _LIVE[db]
        for doc in ({k: v for k, v in live.items() if k != key}, {**live, key: ""}):
            with pytest.raises(
                UpstreamUnavailableError, match=_unreadable(db, live["uid"], f"no {key}$")
            ):
                await _search(monkeypatch, db, doc)
    empty = ("", None, None, [])
    geo = await _search(monkeypatch, "gds", {"accession": "GDS6063"})
    assert (geo.id, geo.accessions) == ("geo:GDS6063", ["GDS6063"])
    assert (geo.title, geo.year, geo.description, geo.organism) == empty
    bp = await _search(monkeypatch, "bioproject", {"project_acc": "PRJNA1537786"})
    assert (bp.id, bp.accessions) == ("bioproject:PRJNA1537786", ["PRJNA1537786"])
    assert (bp.title, bp.year, bp.description, bp.organism) == empty
    sra = await _search(monkeypatch, "sra", {"expxml": '<Experiment acc="SRX1"/>'})
    assert (sra.id, sra.accessions) == ("sra:SRX1", ["SRX1"])
    assert (sra.title, sra.year, sra.description, sra.organism) == empty


async def test_an_sra_summary_whose_xml_cannot_be_read_is_refused(monkeypatch) -> None:
    """``expxml``/``runs`` are XML fragments. Unparseable XML escaped as a bare
    ``ParseError``, an entity declaration as defusedxml's ``EntitiesForbidden``, and an
    experiment without an accession became the record ``sra:None`` or ``sra:``.
    Positive control: the live summary reads every accession it carries."""
    bomb = '<!DOCTYPE r [<!ENTITY a "aaaa">]><Experiment acc="SRX1">&a;</Experiment>'
    cases = {
        ("expxml", "<Experiment acc='SRX1'>"): r"expxml is not XML \(",
        ("runs", '<Run acc="SRR1">'): r"runs is not XML \(",
        ("expxml", bomb): r"expxml is not XML \(",
        ("expxml", "<Summary><Title>t</Title></Summary>"): "expxml names no Experiment accession$",
        ("expxml", '<Experiment name="x"/>'): "expxml names no Experiment accession$",
        ("expxml", '<Experiment acc=""/>'): "expxml names no Experiment accession$",
    }
    for (key, xml), why in cases.items():
        with pytest.raises(UpstreamUnavailableError, match=_unreadable("sra", "47143239", why)):
            await _search(monkeypatch, "sra", {**_SRA, key: xml})
    assert (await _search(monkeypatch, "sra", _SRA)).accessions == [
        "SRX35511800",
        "SRP741891",
        "PRJNA1538143",
        "SRR40983380",
    ]


async def test_resolve_refuses_a_wrong_typed_ftplink_or_bioproject_uid(monkeypatch) -> None:
    """Resolve reads two fields beside the record: GEO's ``ftplink`` (the supplementary
    directory) and a BioProject's ``uid`` (to follow its SRA links). A wrong type reached
    the FTP lister or elink unchecked. Positive control: the live summaries resolve."""
    asked: dict[str, object] = {}
    docs: dict[str, dict] = {}

    async def fake_esearch(client, db, term, *, retmax, retstart=0):
        return 1, ["1"]

    async def fake_esummary(client, db, ids):
        return [docs[db]]

    async def fake_suppl(client, ftplink):
        asked["ftplink"] = ftplink
        return []

    async def fake_elink(client, *, dbfrom, db, ids):
        asked["uid"] = ids
        return []

    monkeypatch.setattr(omics._eutils, "esearch", fake_esearch)
    monkeypatch.setattr(omics._eutils, "esummary", fake_esummary)
    monkeypatch.setattr(omics._eutils, "elink", fake_elink)
    monkeypatch.setattr(omics.geo, "supplementary_files", fake_suppl)

    docs.update(gds=_GDS, bioproject=_BIOPROJECT)
    await omics.resolve(None, "geo:GDS6063")
    await omics.resolve(None, "bioproject:PRJNA1537786")
    assert asked == {"ftplink": _GDS["ftplink"], "uid": ["1537786"]}
    docs.update(gds={k: v for k, v in _GDS.items() if k != "ftplink"})
    await omics.resolve(None, "geo:GDS6063")
    assert asked["ftplink"] == ""  # no ftplink: nothing to list

    for wrong in (5, None, ["ftp://x/"]):
        docs.update(gds={**_GDS, "ftplink": wrong})
        with pytest.raises(UpstreamUnavailableError, match=r"\(gds\) .*'6063': ftplink is "):
            await omics.resolve(None, "geo:GDS6063")
    for doc, why in (
        ({**_BIOPROJECT, "uid": 1537786}, "uid is int, not text"),
        ({k: v for k, v in _BIOPROJECT.items() if k != "uid"}, "no uid"),
    ):
        docs.update(bioproject=doc)
        with pytest.raises(UpstreamUnavailableError, match=f"\\(bioproject\\) .*: {why}$"):
            await omics.resolve(None, "bioproject:PRJNA1537786")


@live_only
async def test_live_each_db_answers_its_largest_page_and_every_summary_reads() -> None:
    """Each NCBI db at the largest page the adapter asks for (``MAX_SIZE``): every
    summary passes the field checks and becomes a record of that db."""
    async with httpx.AsyncClient() as client:
        for prefix in omics.SUBSOURCES:
            total, recs = await omics.search_subsource(
                client, prefix, "RNA-seq", size=omics.MAX_SIZE
            )
            assert total > omics.MAX_SIZE, prefix
            assert len(recs) == omics.MAX_SIZE, prefix
            assert all(r.id.startswith(f"{prefix}:") and r.accessions for r in recs), prefix
