import os

import httpx
import pytest

from data_aggregator_mcp import omicsdi
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry, compact

_SEARCH = {
    "count": 740517,
    "datasets": [
        {
            "id": "MTBLS1355",
            "source": "metabolights_dataset",
            "title": "Breast Cancer Metabolomics",
            "description": "A metabolomics study.",
        },
        {
            "id": "PXD000001",
            "source": "pride",
            "title": "TMT spike-in",
            "description": "Proteomics.",
        },
        {
            "id": "GSE12345",
            "source": "omics_geo",
            "title": "Some transcriptomics",
            "description": "RNA.",
        },
    ],
}


@pytest.mark.asyncio
async def test_search_asks_omicsdi_for_the_modality_repos_and_reports_its_count():
    async def handler(request):
        assert request.url.path.endswith("/dataset/search")
        assert (
            request.url.params["query"] == f"((cancer OR cancers)) AND {omicsdi._MODALITY_CLAUSE}"
        )
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await omicsdi.search(c, "cancer", size=10)
    # OmicsDI applied the restriction, so its count is the count of what can be paged
    assert total == 740517
    assert [r.id for r in recs][:2] == [
        "omicsdi:metabolights_dataset:MTBLS1355",
        "omicsdi:pride:PXD000001",
    ]
    assert recs[0].source == "omicsdi" and recs[0].kind == "study"
    assert recs[0].files == []


def test_the_modality_clause_names_every_mass_spec_repository():
    assert omicsdi._MODALITY_CLAUSE == (
        'repository:("pride" OR "MassIVE" OR "jPOST" OR "iProX" OR "PeptideAtlas" OR '
        '"PanoramaPublic" OR "MetaboLights" OR "MetabolomicsWorkbench" OR "GNPS")'
    )
    assert omicsdi._modality_query("a b") == f"(a b) AND {omicsdi._MODALITY_CLAUSE}"
    assert omicsdi._modality_query("  ") == omicsdi._MODALITY_CLAUSE


@pytest.mark.asyncio
async def test_search_offset_is_sent_as_start():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await omicsdi.search(c, "x", size=10, offset=10)
        await omicsdi.search(c, "x", size=10)
    assert (total, len(recs)) == (740517, 3)
    assert seen[0]["start"] == "10" and "start" not in seen[1]


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


_MASS_SPEC_SOURCES = {
    "pride",
    "massive",
    "jpost",
    "iprox",
    "peptide_atlas",
    "panorama",
    "metabolights_dataset",
    "metabolomics_workbench",
    "gnps",
}


@_live_only
@pytest.mark.asyncio
async def test_live_search_modality_only_and_pages():
    async with httpx.AsyncClient(timeout=60) as c:
        total, page1 = await omicsdi.search(c, "cancer", size=20)
        total2, page2 = await omicsdi.search(c, "cancer", size=20, offset=20)
    assert {r.id.split(":")[1] for r in page1 + page2} <= _MASS_SPEC_SOURCES
    assert len(page1) == len(page2) == 20 and total == total2 > 40
    assert not {r.id for r in page1} & {r.id for r in page2}  # page 2 is new records


@_live_only
@pytest.mark.asyncio
async def test_live_a_search_the_old_filter_emptied_finds_the_mass_spec_datasets():
    """Round 2: the old after-fetch filter kept 3 of page 1's 10 hits and could not page."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await omicsdi.search(c, "Chlamydomonas nitrogen", size=50)
    assert total >= 30 and len(recs) == total
    assert {r.id.split(":")[1] for r in recs} <= _MASS_SPEC_SOURCES


_RECORD = {"accession": "PXD000001", "name": "TMT spike-in", "description": "Proteomics study."}


@pytest.mark.asyncio
async def test_resolve_pride_routes_to_pride_files(monkeypatch):
    async def fake_pride_files(client, acc):
        assert acc == "PXD000001"
        return [FileEntry(name="a.raw", url="https://ftp.pride.ebi.ac.uk/a.raw", source="pride")]

    monkeypatch.setattr("data_aggregator_mcp.pride.files", fake_pride_files)

    async def handler(request):
        assert request.url.path.endswith("/dataset/pride/PXD000001")
        return httpx.Response(200, json=_RECORD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD000001")
    assert r.id == "omicsdi:pride:PXD000001" and r.title == "TMT spike-in"
    assert [f.name for f in r.files] == ["a.raw"]
    assert any(lnk.rel == "landing_page" for lnk in r.links)


@pytest.mark.asyncio
async def test_resolve_non_fetchable_repo_has_empty_files():
    rec = {"accession": "MSV000001", "name": "x", "description": "y"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rec))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:massive:MSV000001")
    assert r.files == []  # MassIVE is discovery-only this wave


_RECORD_PRIDE_RICH = {
    "accession": "PXD002213",
    "name": "Gastric cancer ascites proteome",
    "description": "Comparative proteomics.",
    "additional": {
        "submitter": ["Dohyun Han"],
        "species": ["Homo Sapiens (human)"],
        "publication": ["29654727 Jin J, Son M. Comparative proteomic analysis."],
    },
}

_RECORD_MTBLS_RICH = {
    "accession": "MTBLS9830",
    "name": "Medulloblastoma metabolome",
    "description": "Multiomic profiling.",
    "additional": {
        "submitter_name": ["Jane Roe"],
        "author": ["Jane Roe", "John Doe"],  # present but NOT a creator source
        "organism": ["Homo sapiens"],
        "publication": ["Multiomic profiling of medulloblastoma. 10.1101/2023.01.09.523234."],
    },
}


@pytest.mark.asyncio
async def test_resolve_enriches_pride_provenance(monkeypatch):
    """D3: PRIDE additional → creators (submitter), organism (species, verbatim),
    pmid from a leading-digit publication token."""
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD_PRIDE_RICH))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD002213")
    assert [cr.name for cr in r.creators] == ["Dohyun Han"]
    assert r.organism == ["Homo Sapiens (human)"]
    assert r.identifiers.get("pmid") == "29654727"


@pytest.mark.asyncio
async def test_resolve_enriches_metabolights_doi_and_submitter_fallback(monkeypatch):
    """D3: a repo using `submitter_name`/`organism` keys still yields creators (the
    depositor, NOT the paper `author` list), organism, and a DOI from publication."""
    # The mock answers every URL with the OmicsDI record, which is no FTP directory index.
    monkeypatch.setattr("data_aggregator_mcp.metabolights.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD_MTBLS_RICH))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:metabolights_dataset:MTBLS9830")
    assert [cr.name for cr in r.creators] == ["Jane Roe"]  # submitter_name, not author
    assert r.organism == ["Homo sapiens"]
    # the publication's DOI is the paper's, not the dataset's
    assert r.doi is None
    assert [lnk.target_id for lnk in r.links if lnk.rel == "described_in"] == [
        "10.1101/2023.01.09.523234"
    ]
    assert r.identifiers.get("pmid") is None  # no leading-digit token


@pytest.mark.asyncio
async def test_resolve_sparse_record_stays_clean(monkeypatch):
    """A record with no `additional` block adds no fabricated creators/organism."""
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD000001")
    assert r.creators == [] and r.organism == [] and r.identifiers == {}


async def _aempty():
    return []


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await omicsdi.resolve(c, "omicsdi:onlytwo")


@_live_only
@pytest.mark.asyncio
async def test_live_search_at_the_largest_size_passes_the_answer_check():
    """A full page of live hits passes `_check_search` (1,561 of 1,561 sampled did)."""
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await omicsdi.search(c, "proteome", size=omicsdi.MAX_SIZE)
    assert total >= len(recs) == omicsdi.MAX_SIZE


@_live_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rid", "doi", "papers", "identifiers"),
    [
        # the PRIDE dataset's own DOI, beside its paper's
        (
            "omicsdi:pride:PXD026702",
            "10.6019/PXD026702",
            ["10.1016/J.CELREP.2022.111241"],
            {},
        ),
        # a leading PMID; the DOI ending the string is a Mol Cell Proteomics article
        ("omicsdi:pride:PXD002724", None, ["10.1074/mcp.M115.055079"], {"pmid": "26419955"}),
        # MetaboLights: "<title>. <doi>. PMID:<pmid>"
        (
            "omicsdi:metabolights_dataset:MTBLS806",
            None,
            ["10.3390/metabo9050095"],
            {"pmid": "31083459"},
        ),
    ],
)
async def test_live_resolve_keeps_a_papers_doi_out_of_the_records_doi(
    monkeypatch, rid, doi, papers, identifiers
):
    # the file listings are PRIDE's and MetaboLights' own boundaries, tested there
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    monkeypatch.setattr("data_aggregator_mcp.metabolights.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(timeout=60) as c:
        r = await omicsdi.resolve(c, rid)
    assert r.doi == doi
    assert [lnk.target_id for lnk in r.links if lnk.rel == "described_in"] == papers
    assert r.identifiers == identifiers


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_an_unknown_or_miscased_accession_is_not_found():
    async with httpx.AsyncClient(timeout=60) as c:
        # positive control: the canonical accession resolves
        assert (await omicsdi.resolve(c, "omicsdi:massive:MSV000081764")).title
        for rid in ("omicsdi:pride:PXD999999999", "omicsdi:massive:msv000081764"):
            with pytest.raises(NotFoundError, match=r"^\[NotFoundError\] OmicsDI has no "):
                await omicsdi.resolve(c, rid)


_RECORD_PROTOCOLS = {
    "accession": "PXD055071",
    "name": "Extracellular Vesicles in Chlamydomonas reinhardtii",
    "description": "Microalgal EVs ... under nutrient stress conditions.",
    "additional": {
        # Out of order on purpose: methods lists them in its own order.
        "data_protocol": ["MaxQuant 2.0."],
        "sample_protocol": [
            " Cells grown in TAP; nitrogen depletion (ND) after 3 days in TAP-N. ",
            "",
            "Cells grown in TAP; nitrogen depletion (ND) after 3 days in TAP-N.",
            "EVs isolated by ultracentrifugation.",
        ],
        "quantification_method": ["label free"],  # not a protocol: left out
    },
}


@pytest.mark.asyncio
async def test_resolve_names_the_sampled_conditions_the_protocols_hold(monkeypatch):
    """PXD055071's description says "nutrient stress"; its nitrogen depletion is only in
    the sample protocol (live record, 2026-10-07)."""
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_RECORD_PROTOCOLS))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD055071")
    assert r.methods == (
        "Sample protocol: Cells grown in TAP; nitrogen depletion (ND) after 3 days in TAP-N. "
        "EVs isolated by ultracentrifugation.\n\n"
        "Data protocol: MaxQuant 2.0."
    )


@pytest.mark.asyncio
async def test_resolve_reads_the_metabolights_protocol_keys(monkeypatch):
    monkeypatch.setattr("data_aggregator_mcp.metabolights.files", lambda c, a: _aempty())
    rec = {
        **_RECORD_MTBLS_RICH,
        "additional": {
            **_RECORD_MTBLS_RICH["additional"],
            "extraction_protocol": ["Methanol extraction."],
            "sample_collection_protocol": ["Plasma after fasting."],
            "study_design": ["Case vs control."],
            "chromatography_protocol": ["HILIC."],  # how it was measured: left out
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rec))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:metabolights_dataset:MTBLS9830")
    assert r.methods == (
        "Study design: Case vs control.\n\n"
        "Sample collection protocol: Plasma after fasting.\n\n"
        "Extraction protocol: Methanol extraction."
    )


@pytest.mark.asyncio
async def test_a_record_with_no_protocol_text_has_no_methods(monkeypatch):
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    blank = {**_RECORD_PRIDE_RICH, "additional": {"sample_protocol": [" ", ""]}}
    for rec in (_RECORD_PRIDE_RICH, blank):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r, rec=rec: httpx.Response(200, json=rec))
        ) as c:
            r = await omicsdi.resolve(c, "omicsdi:pride:PXD002213")
        assert r.methods is None
    # A search hit never carries methods: compact drops them like the file manifest.
    assert compact(r.model_copy(update={"methods": "Sample protocol: x"})).methods is None


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_shows_the_condition_only_the_protocol_names(monkeypatch):
    monkeypatch.setattr("data_aggregator_mcp.pride.files", lambda c, a: _aempty())
    async with httpx.AsyncClient(timeout=60) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD055071")
    assert "nitrogen depletion" in r.methods
    assert r.methods.startswith("Sample protocol: ")
    # Why it matters: the description, all a search hit shows, never says nitrogen.
    assert "nitrogen" not in (r.description or "").lower()
