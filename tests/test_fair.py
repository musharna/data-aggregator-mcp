"""Tests for the FAIR-score enricher (fair.assess).

assess is PURE: no I/O, deterministic. Scoring is grounded in the RDA FAIR Data
Maturity Model machine-evaluable subset; gaps name their RDA indicator id and
are framed as metadata-exposure gaps, not value judgements about the dataset.
"""

from __future__ import annotations

import os

import pytest

from data_aggregator_mcp import fair
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FairAssessment,
    FileEntry,
    FundingRef,
    Link,
    Taxon,
)


def _rich() -> DataResource:
    """A fully-populated resource that should pass (almost) every indicator."""
    return DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="A rich dataset",
        description="A thorough description of the dataset and its methods.",
        creators=[Creator(name="Ada Lovelace", orcid="0000-0002-1825-0097")],
        subjects=["genomics", "rna-seq"],
        doi="10.5281/zenodo.1",
        accessions=["GSE12345"],
        identifiers={"doi": "10.5281/zenodo.1"},
        taxa=[Taxon(taxid=9606, name="Homo sapiens")],
        license="cc-by-4.0",
        access="open",
        funding=[FundingRef(funder="NIH", award="R01-123")],
        last_updated="2024-01-01T00:00:00Z",
        files=[FileEntry(name="data.csv", mime="text/csv", url="https://example.org/data.csv")],
        links=[Link(rel="is_supplement_to", target_id="doi:10.1/x")],
    )


def _bare() -> DataResource:
    """A bare resource: id + title only — should score low."""
    return DataResource(id="x:1", source="x", kind="dataset", title="bare")


# --- model wiring -----------------------------------------------------------


def test_fair_assessment_defaults():
    fa = FairAssessment(score=0, findable=0, accessible=0, interoperable=0, reusable=0, assessed=0)
    assert fa.gaps == []


def test_dataresource_has_optional_fair_field():
    assert _bare().fair is None


# --- coarse behaviour -------------------------------------------------------


def test_rich_resource_scores_high():
    fa = fair.assess(_rich())
    assert isinstance(fa, FairAssessment)
    assert fa.score > 80
    assert fa.findable > 80
    assert fa.accessible > 80
    assert fa.interoperable > 80
    assert fa.reusable > 80


def test_bare_resource_scores_low_with_named_gaps():
    fa = fair.assess(_bare())
    assert fa.score < 50
    # Every gap names an RDA indicator id in parentheses.
    assert fa.gaps
    for gap in fa.gaps:
        assert "(RDA-" in gap and gap.endswith(")")
    # The Essential findable/accessible/reusable indicators must be flagged.
    blob = " ".join(fa.gaps)
    assert "RDA-F1-01D" in blob  # no DOI
    assert "RDA-F2-01M" in blob  # sparse metadata
    assert "RDA-A1-01M" in blob  # no resolvable id / url
    assert "RDA-R1.1-01M" in blob  # no licence


def test_rich_resource_gaps_minimal():
    # Rich has no licence-prose gap, no metadata gaps; only I3-ish/none.
    fa = fair.assess(_rich())
    blob = " ".join(fa.gaps)
    assert "RDA-F1-01D" not in blob
    assert "RDA-R1.1-01M" not in blob
    assert "RDA-R1.1-03M" not in blob  # cc-by-4.0 is machine-readable


# --- assessed count is stable and real --------------------------------------


def test_assessed_equals_indicator_count():
    expected = len(fair.INDICATORS)
    assert fair.assess(_rich()).assessed == expected
    assert fair.assess(_bare()).assessed == expected


# --- per-indicator on/off pairs ---------------------------------------------


def _gap_ids(fa: FairAssessment) -> set[str]:
    out = set()
    for gap in fa.gaps:
        # extract the "(RDA-...)" token
        start = gap.rfind("(RDA-")
        if start != -1:
            out.add(gap[start + 1 : gap.rfind(")")])
    return out


def test_f1_doi_flips_gap_and_subscore():
    on = fair.assess(_rich())
    off = fair.assess(_rich().model_copy(update={"doi": None, "identifiers": {}}))
    assert "RDA-F1-01D" not in _gap_ids(on)
    assert "RDA-F1-01D" in _gap_ids(off)
    assert off.findable < on.findable


def test_f2_rich_metadata_predicate():
    off = fair.assess(_rich().model_copy(update={"description": None}))
    assert "RDA-F2-01M" in _gap_ids(off)


def test_f3_data_identifier_predicate():
    off = fair.assess(_bare().model_copy(update={"doi": None, "accessions": [], "identifiers": {}}))
    assert "RDA-F3-01M" in _gap_ids(off)
    on = fair.assess(_bare().model_copy(update={"accessions": ["GSE1"]}))
    assert "RDA-F3-01M" not in _gap_ids(on)


def test_a1_retrievable_predicate():
    off = fair.assess(_bare())  # no doi, no file url
    assert "RDA-A1-01M" in _gap_ids(off)
    on = fair.assess(
        _bare().model_copy(update={"files": [FileEntry(name="d", url="https://e.org/d")]})
    )
    assert "RDA-A1-01M" not in _gap_ids(on)


def test_a11_open_protocol_predicate():
    off = fair.assess(
        _bare().model_copy(update={"files": [FileEntry(name="d", url="ftp://e.org/d")]})
    )
    assert "RDA-A1.1-01M" in _gap_ids(off)
    on = fair.assess(_rich())  # https + doi
    assert "RDA-A1.1-01M" not in _gap_ids(on)


def test_i1_file_format_predicate():
    off = fair.assess(
        _rich().model_copy(update={"files": [FileEntry(name="d", url="https://e/d")]})
    )
    assert "RDA-I1-01M" in _gap_ids(off)
    assert "RDA-I1-01M" not in _gap_ids(fair.assess(_rich()))


def test_i2_vocab_predicate():
    off = fair.assess(_bare())  # no taxa, no subjects
    assert "RDA-I2-01M" in _gap_ids(off)
    on = fair.assess(_bare().model_copy(update={"subjects": ["x"]}))
    assert "RDA-I2-01M" not in _gap_ids(on)


def test_i3_links_predicate():
    off = fair.assess(_rich().model_copy(update={"links": []}))
    assert "RDA-I3-01M" in _gap_ids(off)
    assert "RDA-I3-01M" not in _gap_ids(fair.assess(_rich()))


def test_r12_provenance_predicate():
    off = fair.assess(
        _rich().model_copy(update={"funding": [], "last_updated": None, "links": [], "source": ""})
    )
    assert "RDA-R1.2-01M" in _gap_ids(off)
    assert "RDA-R1.2-01M" not in _gap_ids(fair.assess(_rich()))


def test_r13_community_standard_predicate():
    # No accessions and no known extension → gap.
    off = fair.assess(
        _bare().model_copy(update={"files": [FileEntry(name="d.bin", url="https://e/d")]})
    )
    assert "RDA-R1.3-01M" in _gap_ids(off)
    on = fair.assess(_bare().model_copy(update={"accessions": ["GSE1"]}))
    assert "RDA-R1.3-01M" not in _gap_ids(on)
    ext = fair.assess(
        _bare().model_copy(update={"files": [FileEntry(name="d.fastq", url="https://e/d")]})
    )
    assert "RDA-R1.3-01M" not in _gap_ids(ext)


# --- R1.1a vs R1.1b are DISTINCT --------------------------------------------


@pytest.mark.parametrize("lic", ["cc-by-4.0", "MIT", "CC0-1.0", "Apache-2.0", "GPL-3.0"])
def test_machine_readable_licence_passes_both(lic):
    fa = fair.assess(_bare().model_copy(update={"license": lic}))
    ids = _gap_ids(fa)
    assert "RDA-R1.1-01M" not in ids  # present
    assert "RDA-R1.1-03M" not in ids  # machine-readable


@pytest.mark.parametrize("lic", ["see LICENSE.txt", "Contact authors", "All rights reserved"])
def test_free_text_licence_passes_a_fails_b(lic):
    fa = fair.assess(_bare().model_copy(update={"license": lic}))
    ids = _gap_ids(fa)
    assert "RDA-R1.1-01M" not in ids  # R1.1a: a licence IS present
    assert "RDA-R1.1-03M" in ids  # R1.1b: but not machine-readable


def test_no_licence_fails_both():
    fa = fair.assess(_bare())
    ids = _gap_ids(fa)
    assert "RDA-R1.1-01M" in ids
    assert "RDA-R1.1-03M" in ids


# --- score math (pin the formula) -------------------------------------------


def test_score_math_exact_constructed_subset():
    """Construct a resource hitting a known weighted subset; assert exact ints.

    Findable indicators (weights): F1=3(E), F2=3(E), F3=2(I), F4=2(I const True).
    Build a resource passing F3 + F4 only (accession, no doi, sparse metadata):
      passed weight = 2 (F3) + 2 (F4) = 4; total = 3+3+2+2 = 10
      findable = round(100 * 4/10) = 40.

    Accessible: A1=3(E), A1.1=2(I), A2=2(I const True). With a file url:
      A1 pass(3), A1.1 pass(2) [https], A2 True(2) → passed=7 total=7 → 100.

    Interoperable: I1=3(E format), I2=1(U vocab), I3=2(I links). file has no mime,
      no taxa, subjects present (I2 pass weight 1), no links:
      passed = 1 (I2); total = 3+1+2 = 6 → round(100*1/6)=17.

    Reusable: R1.1a=3(E), R1.1b=2(I), R1.2=2(I), R1.3=1(U).
      no licence (R1.1a fail, R1.1b fail), creators absent (R1.2 fail since needs
      creators), accession present (R1.3 pass):
      passed = 1 (R1.3); total = 3+2+2+1 = 8 → round(100*1/8)=12.

    overall = round(mean(40,100,17,12)) = round(169/4)=round(42.25)=42.
    """
    r = DataResource(
        id="x:1",
        source="x",
        kind="dataset",
        title="t",
        description=None,  # F2 fails
        creators=[],  # R1.2 fails
        subjects=["genomics"],  # I2 passes
        accessions=["GSE1"],  # F3 + R1.3 pass
        files=[FileEntry(name="d", url="https://e.org/d")],  # A1/A1.1 pass, I1 fail
    )
    fa = fair.assess(r)
    assert fa.findable == 40
    assert fa.accessible == 100
    assert fa.interoperable == 17
    assert fa.reusable == 12
    assert fa.score == 42


# --- purity / determinism ---------------------------------------------------


def test_assess_is_deterministic():
    r = _rich()
    assert fair.assess(r).model_dump() == fair.assess(r).model_dump()


def test_assess_takes_only_resource_no_client():
    # assess has a single positional parameter (the resource) — no client/IO arg.
    import inspect

    params = list(inspect.signature(fair.assess).parameters)
    assert params == ["resource"]


# --- live real-execution check ----------------------------------------------

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_rich_outscores_sparse():
    """Assess two REAL resolved records: a rich Zenodo DOI and a sparser GEO
    accession. Rich must outscore sparse and gaps must be plausible."""
    import httpx

    from data_aggregator_mcp import router

    async with httpx.AsyncClient(timeout=60) as c:
        rich = await router.resolve(c, "10.5281/zenodo.3242074")
        sparse = await router.resolve(c, "geo:GSE100866")
    rich_fa = fair.assess(rich)
    sparse_fa = fair.assess(sparse)
    assert rich_fa.assessed == sparse_fa.assessed == len(fair.INDICATORS)
    assert rich_fa.score > sparse_fa.score
    for gap in rich_fa.gaps + sparse_fa.gaps:
        assert "(RDA-" in gap
