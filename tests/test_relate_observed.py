"""`relate.detect` output pinned whole: kind, resources and their order, key, evidence and
suggestion, for each detector and for the inputs real adapters produce.

Live checks (``DATA_AGGREGATOR_MCP_LIVE=1``) drive `router.relate` over real records, the
resolve fan-out being the only network boundary `relate` has.
"""

from __future__ import annotations

import os
import subprocess
import sys

import httpx
import pytest

from data_aggregator_mcp import relate as relate_mod
from data_aggregator_mcp import router
from data_aggregator_mcp.models import JoinHint
from tests.test_relate import _res

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _ident_hint(scheme: str, key: str, ids: list[str]) -> JoinHint:
    return JoinHint(
        kind="shared_identifier",
        resources=ids,
        key=key,
        evidence=f"{scheme} {key!r} shared by {len(ids)} resources",
        suggestion=f"same work or paper-data link via {scheme} {key}",
    )


# --- shared_identifier: work ids only, compared within their scheme -------------------


def test_shared_identifier_ignores_uniprot_taxid_and_gene() -> None:
    # What uniprot._normalize puts in `identifiers`: two human proteins share taxon 9606,
    # and two species' TP53 entries share the gene name. Neither makes them one work.
    rs = [
        _res("uniprot:P04637", identifiers={"taxid": "9606", "gene": "TP53"}),
        _res("uniprot:P38398", identifiers={"taxid": "9606", "gene": "BRCA1"}),
        _res("uniprot:Q00366", identifiers={"taxid": "10090", "gene": "TP53"}),
        # positive control: a shared PubMed id among the same records still joins
        _res("gwas:GCST1", identifiers={"pmid": "15761122"}),
        _res("pubmed:15761122", identifiers={"pmid": "15761122"}),
    ]
    assert relate_mod.detect(rs) == [
        _ident_hint("pmid", "15761122", ["gwas:GCST1", "pubmed:15761122"])
    ]


def test_shared_identifier_does_not_join_across_schemes() -> None:
    # PubMed article 9606 and NCBI taxon 9606, PMID 5 and a DOI ending "5": one value,
    # different schemes, unrelated records.
    rs = [
        _res("pubmed:9606", identifiers={"pmid": "9606"}),
        _res("uniprot:P04637", identifiers={"taxid": "9606"}),
        _res("pubmed:5", identifiers={"pmid": "5", "pmcid": "PMC77"}),
        _res("zenodo:5", doi="5"),
        # positive control: the PMCID is shared within its scheme, case folded
        _res("biostudies:S-EPMC77", identifiers={"pmcid": "pmc77"}),
    ]
    assert relate_mod.detect(rs) == [
        _ident_hint("pmcid", "PMC77", ["pubmed:5", "biostudies:S-EPMC77"])
    ]


def test_shared_identifier_ignores_biostudies_xref_types() -> None:
    # biostudies._normalize stores each cross-reference both in `accessions` and under its
    # type in `identifiers`; the accession detector reports it once, as an accession.
    rs = [
        _res("biostudies:S-1", accessions=["S-1", "GSE5"], identifiers={"geo": "GSE5"}),
        _res("biostudies:S-2", accessions=["S-2", "GSE5"], identifiers={"geo": "GSE5"}),
    ]
    assert relate_mod.detect(rs) == [
        JoinHint(
            kind="shared_accession",
            resources=["biostudies:S-1", "biostudies:S-2"],
            key="GSE5",
            evidence="accession 'GSE5' present on 2 resources",
            suggestion="joinable on accession GSE5",
        )
    ]


def _paper_and_study() -> list:
    # one paper, one study citing it; each id given twice, in different forms and orders
    return [
        _res(
            "pubmed:5",
            doi="10.1/X",
            identifiers={"pmcid": "PMC9", "pmid": "5", "doi": "https://doi.org/10.1/x"},
        ),
        _res("gwas:G1", doi="doi:10.1/x", identifiers={"pmid": "5", "pmcid": "pmc9"}),
    ]


def test_shared_identifier_order_and_shown_form_are_fixed() -> None:
    # The `doi` field first, then doi/pmid/pmcid; each key shown as first given.
    assert relate_mod.detect(_paper_and_study()) == [
        _ident_hint("doi", "10.1/X", ["pubmed:5", "gwas:G1"]),
        _ident_hint("pmid", "5", ["pubmed:5", "gwas:G1"]),
        _ident_hint("pmcid", "PMC9", ["pubmed:5", "gwas:G1"]),
    ]


_HASH_PROBE = (
    "from tests.test_relate_observed import _paper_and_study\n"
    "from data_aggregator_mcp import relate\n"
    "print([(h.key, h.evidence) for h in relate.detect(_paper_and_study())])\n"
)


def test_shared_identifier_output_does_not_depend_on_the_hash_seed() -> None:
    # Iterating a set of strings made the hint order and the shown form vary with
    # PYTHONHASHSEED, so one server process answered differently from the next.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    outs = {
        subprocess.run(
            [sys.executable, "-c", _HASH_PROBE],
            env={**env, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        for seed in range(1, 9)
    }
    assert outs == {
        "[('10.1/X', \"doi '10.1/X' shared by 2 resources\"), "
        "('5', \"pmid '5' shared by 2 resources\"), "
        "('PMC9', \"pmcid 'PMC9' shared by 2 resources\")]\n"
    }


@live_only
async def test_live_relate_joins_a_paper_to_its_study_and_not_proteins_by_taxon() -> None:
    """Real records: UniProt P04637 (TP53) and P38398 (BRCA1) are both human, and
    PubMed 9606 is an unrelated 1970s article whose PMID equals the human taxon id.
    The GWAS study's PMID is read from its resolved record, so the positive control
    cannot fail on upstream data."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # the premise: two proteins share a taxon id that is also an article's PMID
        tp53, brca1, article, study = [
            await router.resolve(client, rid)
            for rid in ("uniprot:P04637", "uniprot:P38398", "pubmed:9606", "gwas:GCST000001")
        ]
        assert tp53.identifiers["taxid"] == brca1.identifiers["taxid"] == "9606"
        assert article.identifiers["pmid"] == "9606"
        pmid = study.identifiers["pmid"]
        out = await router.relate(
            client,
            [
                "uniprot:P04637",
                "uniprot:P38398",
                "pubmed:9606",
                "gwas:GCST000001",
                f"pubmed:{pmid}",
            ],
        )
    assert out.errors == {}, out.errors
    idents = [h for h in out.hints if h.kind == "shared_identifier"]
    assert idents == [_ident_hint("pmid", pmid, ["gwas:GCST000001", f"pubmed:{pmid}"])], out.hints


# --- landing-page URLs: each form a real record or landing page uses ------------------


@pytest.mark.parametrize(
    ("url", "rid"),
    [
        # zenodo's current form, and the legacy form real related identifiers still use
        ("https://zenodo.org/records/7421899", "zenodo:7421899"),
        ("https://zenodo.org/record/7391890#.Y5STINLMLlg", "zenodo:7391890"),
        ("https://sandbox.zenodo.org/records/2", "https://sandbox.zenodo.org/records/2"),
        # PubMed's form, and NCBI's (301 to PubMed; ICPSR, NEMAR and Zenodo records cite it)
        ("https://pubmed.ncbi.nlm.nih.gov/31234567/", "pubmed:31234567"),
        ("https://www.ncbi.nlm.nih.gov/pubmed/17254445", "pubmed:17254445"),
        ("http://ncbi.nlm.nih.gov/pubmed/17254445", "pubmed:17254445"),
        (
            "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE100000",
            "geo:GSE100000",
        ),
        ("https://www.ncbi.nlm.nih.gov/bioproject/PRJNA257197/", "bioproject:PRJNA257197"),
        ("https://www.ncbi.nlm.nih.gov/sra/SRX1000000", "sra:SRX1000000"),
        ("https://www.ebi.ac.uk/gwas/studies/GCST000001", "gwas:GCST000001"),
        # OpenML: /d/<id>, and the search page it redirects to, parameters in any order
        ("https://www.openml.org/d/47276", "openml:47276"),
        ("https://openml.org/search?type=data&status=active&id=40669", "openml:40669"),
        ("https://www.openml.org/search?type=data&sort=runs&id=61", "openml:61"),
        ("https://www.openml.org/search?id=61&type=data", "openml:61"),
        ("https://www.openml.org/search?type=data&id=61#runs", "openml:61"),
        # a task with the same number, or look-alike parameters, are not dataset 61
        (
            "https://www.openml.org/search?type=task&id=61",
            "https://www.openml.org/search?type=task&id=61",
        ),
        (
            "https://www.openml.org/search?type=data&task_id=61",
            "https://www.openml.org/search?type=data&task_id=61",
        ),
        (
            "https://www.openml.org/search?subtype=data&id=61",
            "https://www.openml.org/search?subtype=data&id=61",
        ),
        # RCSB: an entry cited before its release keeps its unreleased/ URL (now a 404)
        ("https://www.rcsb.org/structure/4HHB", "pdb:4HHB"),
        ("https://www.rcsb.org/structure/unreleased/8CJN", "pdb:8CJN"),
        ("https://www.rcsb.org/structure/unreleased", "https://www.rcsb.org/structure/unreleased"),
        ("https://dandiarchive.org/dandiset/000003", "dandi:000003"),
        ("https://dandiarchive.org/dandiset/000003/0.230629.1955", "dandi:000003"),
        ("https://huggingface.co/datasets/le-teen/college-roi-data", "hf:le-teen/college-roi-data"),
        ("https://huggingface.co/datasets/o/n?row=3", "hf:o/n"),
        ("  https://www.openml.org/d/61  ", "openml:61"),
        ("10.5281/zenodo.2", "10.5281/zenodo.2"),
        ("", ""),
        (None, None),
    ],
)
def test_url_to_id_maps_each_real_landing_form(url: str | None, rid: str | None) -> None:
    assert relate_mod._url_to_id(url) == rid


@live_only
async def test_live_relate_links_records_cited_by_landing_url() -> None:
    """Real records whose metadata names another record only by its landing-page URL:
    an ICPSR study cited by a PubMed article (NCBI's /pubmed/ form), a Zenodo deposit
    referencing an OpenML dataset (OpenML's search-page form), and a Zenodo deposit
    identical to a Hugging Face and an OpenML dataset. Each link is read from the
    resolved record first, so a changed upstream fails the premise, not `relate`."""
    cases = [
        (
            "datacite:10.3886/icpsr00001.v4",
            "https://www.ncbi.nlm.nih.gov/pubmed/17254445",
            "pubmed:17254445",
        ),
        (
            "zenodo:22743095",
            "https://openml.org/search?type=data&status=active&id=40669",
            "openml:40669",
        ),
        (
            "zenodo:22945909",
            "https://huggingface.co/datasets/le-teen/college-roi-data",
            "hf:le-teen/college-roi-data",
        ),
        ("zenodo:22945909", "https://www.openml.org/d/47276", "openml:47276"),
    ]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for src, url, target in cases:
            rec = await router.resolve(client, src)
            rels = [link.rel for link in rec.links if link.target_id == url]
            assert len(rels) == 1, f"{src} no longer links {url}: {rec.links}"
            out = await router.relate(client, [src, target])
            assert out.errors == {}, out.errors
            assert out.hints == [
                JoinHint(
                    kind="explicit_link",
                    resources=[src, target],
                    key=rels[0],
                    evidence=f"{src} links to {target} via {rels[0]!r} (target_id={url!r})",
                    suggestion=f"{src} {rels[0]} {target} (declared in source metadata)",
                )
            ], out.hints
