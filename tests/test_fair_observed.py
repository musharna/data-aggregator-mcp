"""Observed behaviour of the FAIR-score enricher (fair.assess), pinned exactly.

The indicator table is checked against Table 1 of the RDA FAIR Data Maturity Model
specification v1.0 (https://doi.org/10.15497/rda00050, Zenodo record 3909563),
copied here by hand from the PDF, not from the code.
"""

from __future__ import annotations

import pytest

from data_aggregator_mcp import fair
from data_aggregator_mcp.fair import ESSENTIAL, IMPORTANT, USEFUL
from data_aggregator_mcp.models import Creator, FileEntry, FundingRef, Link
from tests.test_fair import _bare, _gap_ids, _live_only, _rich

# Table 1 "FAIR data maturity model indicators" (pp. 11-12), all 41 rows.
RDA_V1_TABLE_1 = {
    "RDA-F1-01M": ESSENTIAL,
    "RDA-F1-01D": ESSENTIAL,
    "RDA-F1-02M": ESSENTIAL,
    "RDA-F1-02D": ESSENTIAL,
    "RDA-F2-01M": ESSENTIAL,
    "RDA-F3-01M": ESSENTIAL,
    "RDA-F4-01M": ESSENTIAL,
    "RDA-A1-01M": IMPORTANT,
    "RDA-A1-02M": ESSENTIAL,
    "RDA-A1-02D": ESSENTIAL,
    "RDA-A1-03M": ESSENTIAL,
    "RDA-A1-03D": ESSENTIAL,
    "RDA-A1-04M": ESSENTIAL,
    "RDA-A1-04D": ESSENTIAL,
    "RDA-A1-05D": IMPORTANT,
    "RDA-A1.1-01M": ESSENTIAL,
    "RDA-A1.1-01D": IMPORTANT,
    "RDA-A1.2-01D": USEFUL,
    "RDA-A2-01M": ESSENTIAL,
    "RDA-I1-01M": IMPORTANT,
    "RDA-I1-01D": IMPORTANT,
    "RDA-I1-02M": IMPORTANT,
    "RDA-I1-02D": IMPORTANT,
    "RDA-I2-01M": IMPORTANT,
    "RDA-I2-01D": USEFUL,
    "RDA-I3-01M": IMPORTANT,
    "RDA-I3-01D": USEFUL,
    "RDA-I3-02M": USEFUL,
    "RDA-I3-02D": USEFUL,
    "RDA-I3-03M": IMPORTANT,
    "RDA-I3-04M": USEFUL,
    "RDA-R1-01M": ESSENTIAL,
    "RDA-R1.1-01M": ESSENTIAL,
    "RDA-R1.1-02M": IMPORTANT,
    "RDA-R1.1-03M": IMPORTANT,
    "RDA-R1.2-01M": IMPORTANT,
    "RDA-R1.2-02M": USEFUL,
    "RDA-R1.3-01M": ESSENTIAL,
    "RDA-R1.3-01D": ESSENTIAL,
    "RDA-R1.3-02M": ESSENTIAL,
    "RDA-R1.3-02D": IMPORTANT,
}

_DIM_LETTER = {"findable": "F", "accessible": "A", "interoperable": "I", "reusable": "R"}


def _ids(r) -> set[str]:
    return _gap_ids(fair.assess(r))


def _fails(r, family: str) -> bool:
    """True when an indicator of ``family`` (e.g. ``"R1.3"``) is a gap. Behaviour tests
    match the family, not the M/D id, which test_indicators_carry_the_v1_ids_and_priorities
    pins on its own."""
    return any(i.startswith(f"RDA-{family}-") for i in _ids(r))


# --- the indicator table is the specification's ------------------------------


def test_priority_weights():
    assert (ESSENTIAL, IMPORTANT, USEFUL) == (3, 2, 1)


def test_indicators_carry_the_v1_ids_and_priorities():
    assert len(RDA_V1_TABLE_1) == 41
    table = [(i.dim, i.rda_id, i.weight) for i in fair.INDICATORS]
    assert table == [
        ("findable", "RDA-F1-01D", ESSENTIAL),
        ("findable", "RDA-F2-01M", ESSENTIAL),
        ("findable", "RDA-F3-01M", ESSENTIAL),
        ("findable", "RDA-F4-01M", ESSENTIAL),
        ("accessible", "RDA-A1-01M", IMPORTANT),
        ("accessible", "RDA-A1.1-01D", IMPORTANT),
        ("accessible", "RDA-A2-01M", ESSENTIAL),
        ("interoperable", "RDA-I1-01D", IMPORTANT),
        ("interoperable", "RDA-I2-01M", IMPORTANT),
        ("interoperable", "RDA-I3-01M", IMPORTANT),
        ("reusable", "RDA-R1.1-01M", ESSENTIAL),
        ("reusable", "RDA-R1.1-03M", IMPORTANT),
        ("reusable", "RDA-R1.2-01M", IMPORTANT),
        ("reusable", "RDA-R1.3-01D", ESSENTIAL),
    ]
    for ind in fair.INDICATORS:
        assert RDA_V1_TABLE_1[ind.rda_id] == ind.weight, ind.rda_id
        assert ind.rda_id.startswith("RDA-" + _DIM_LETTER[ind.dim]), ind.rda_id
        assert ind.gap.endswith(f"({ind.rda_id})"), ind.gap


def test_every_dimension_has_indicators():
    assert {i.dim for i in fair.INDICATORS} == set(_DIM_LETTER)


# --- exact assessments of representative records -----------------------------


def test_bare_record_exact_assessment():
    fa = fair.assess(_bare())
    # F: F4 (3) of 12 = 25; A: A2 (3) of 7 = 43; I: 0 of 6; R: 0 of 10; mean 17.
    assert fa.model_dump() == {
        "score": 17,
        "findable": 25,
        "accessible": 43,
        "interoperable": 0,
        "reusable": 0,
        "assessed": 14,
        "gaps": [
            "no DOI/persistent identifier (RDA-F1-01D)",
            "sparse metadata: needs description + creators/subjects (RDA-F2-01M)",
            "metadata exposes no resolvable data identifier (RDA-F3-01M)",
            "no resolvable identifier or download URL (RDA-A1-01M)",
            "no DOI or download URL over a free protocol (http/https/ftp) (RDA-A1.1-01D)",
            "no machine-readable file formats declared (RDA-I1-01D)",
            "no controlled-vocabulary terms (taxa/subjects) (RDA-I2-01M)",
            "no qualified links to related records (RDA-I3-01M)",
            "no reuse licence (RDA-R1.1-01M)",
            "no machine-readable licence id (RDA-R1.1-03M)",
            "thin provenance: needs creators + (funding/dateModified/relations) (RDA-R1.2-01M)",
            "no recognised community-standard format (RDA-R1.3-01D)",
        ],
    }


def test_rich_record_exact_assessment():
    assert fair.assess(_rich()).model_dump() == {
        "score": 100,
        "findable": 100,
        "accessible": 100,
        "interoperable": 100,
        "reusable": 100,
        "assessed": 14,
        "gaps": [],
    }


def test_zenodo_fastq_deposit_exact_assessment():
    """Shaped like live zenodo:7307258: a DOI, one creator, a CC-BY licence and one
    FASTQ file with no MIME type, no subjects, links, funding or modified date."""
    r = _bare().model_copy(
        update={
            "id": "zenodo:7307258",
            "source": "zenodo",
            "description": "HG006 reads",
            "creators": [Creator(name="A")],
            "doi": "10.5281/zenodo.7307258",
            "license": "cc-by-4.0",
            "files": [FileEntry(name="HG006_Father.R1.fastq.gz", url="https://zenodo.org/f")],
        }
    )
    fa = fair.assess(r)
    # R: R1.1-01M 3 + R1.1-03M 2 + R1.3-01D 3 = 8 of 10; mean(100, 100, 0, 80) = 70.
    assert (fa.score, fa.findable, fa.accessible, fa.interoperable, fa.reusable) == (
        70,
        100,
        100,
        0,
        80,
    )
    assert fa.gaps == [
        "no machine-readable file formats declared (RDA-I1-01D)",
        "no controlled-vocabulary terms (taxa/subjects) (RDA-I2-01M)",
        "no qualified links to related records (RDA-I3-01M)",
        "thin provenance: needs creators + (funding/dateModified/relations) (RDA-R1.2-01M)",
    ]


# --- defects: what each indicator is credited on ------------------------------


def test_r12_source_is_not_provenance():
    # Every record names the adapter that served it; that is not provenance.
    thin = _bare().model_copy(update={"creators": [Creator(name="A")], "source": "zenodo"})
    assert "RDA-R1.2-01M" in _ids(thin)
    for extra in (
        {"funding": [FundingRef(funder="NIH")]},
        {"last_updated": "2024-01-01"},
        {"links": [Link(rel="is_supplement_to", target_id="doi:10.1/x")]},
    ):
        assert "RDA-R1.2-01M" not in _ids(thin.model_copy(update=extra)), extra
    # Creators are required whatever else is present.
    no_creators = _rich().model_copy(update={"creators": []})
    assert "RDA-R1.2-01M" in _ids(no_creators)


@pytest.mark.parametrize(
    ("name", "standard"),
    [
        ("HG006_Father.R1.fastq.gz", True),
        ("counts.tsv.gz", True),
        ("calls.vcf.bz2", True),
        ("tracks.bed.xz", True),
        ("table.CSV.ZST", True),
        ("reads.fastq", True),
        ("blob.bin.gz", False),
        ("bundle.tar.gz", False),
        ("table.csv.zip", False),
        ("blob.gz", False),
        ("table.csv.gz.bak", False),
    ],
)
def test_r13_reads_the_format_under_a_compression_suffix(name, standard):
    r = _bare().model_copy(update={"files": [FileEntry(name=name, url="https://e/d")]})
    assert (not _fails(r, "R1.3")) is standard


@pytest.mark.parametrize(
    ("url", "free"),
    [
        ("https://e.org/d", True),
        ("http://www.oig.doc.gov/Pages/U.S.-Census-Bureau.aspx", True),
        ("ftp://ftp.ncbi.nlm.nih.gov/geo/x.gz", True),
        ("HTTPS://E.ORG/D", True),
        ("s3://bucket/d", False),
        ("globus://endpoint/d", False),
        ("file:///tmp/d", False),
    ],
)
def test_a11_http_and_ftp_are_free_protocols(url, free):
    r = _bare().model_copy(update={"files": [FileEntry(name="d", url=url)]})
    assert not _fails(r, "A1")  # a URL is there whatever its scheme
    assert (not _fails(r, "A1.1")) is free


def test_a11_doi_alone_is_a_free_protocol():
    assert not _fails(_bare().model_copy(update={"doi": "10.1/x"}), "A1.1")
    url_less = _bare().model_copy(update={"files": [FileEntry(name="d")]})
    assert _fails(url_less, "A1.1")


def test_r11_03_gap_does_not_call_a_missing_licence_free_text():
    gap = "no machine-readable licence id (RDA-R1.1-03M)"
    assert gap in fair.assess(_bare()).gaps
    assert gap in fair.assess(_bare().model_copy(update={"license": "see LICENSE.txt"})).gaps
    assert gap not in fair.assess(_bare().model_copy(update={"license": "MIT"})).gaps


# --- live: records whose shape the defects were found on ----------------------


@_live_only
@pytest.mark.asyncio
async def test_live_records_are_credited_for_what_they_hold():
    """A Zenodo deposit of one ``.fastq.gz`` (no accessions) and a data.gov record
    whose only download URL is ``http://`` (no DOI), as the adapters return them."""
    import httpx

    from data_aggregator_mcp import router

    async with httpx.AsyncClient(timeout=60) as c:
        fastq = await router.resolve(c, "zenodo:7307258")
        http_only = await router.resolve(c, "datagov:oversight-areas-u-s-census-bureau")

    assert not fastq.accessions
    assert any(f.name.endswith(".fastq.gz") for f in fastq.files)
    assert not _fails(fastq, "R1.3")
    # The adapter's own ``source`` does not stand in for provenance.
    assert fastq.source == "zenodo"
    stripped = fastq.model_copy(update={"funding": [], "last_updated": None, "links": []})
    assert fastq.creators
    assert "RDA-R1.2-01M" in _ids(stripped)

    assert http_only.doi is None
    assert [f.url for f in http_only.files if f.url]
    assert not _fails(http_only, "A1.1")
