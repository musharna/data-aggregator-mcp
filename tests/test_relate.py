from __future__ import annotations

from data_aggregator_mcp import relate as relate_mod
from data_aggregator_mcp.models import DataResource, JoinHint, RelateResult


def _res(rid: str, **kw) -> DataResource:
    # minimal valid DataResource; required fields: id, source, kind, title
    base = dict(id=rid, source=rid.split(":")[0], kind="dataset", title=rid)
    base.update(kw)
    return DataResource(**base)


def test_joinhint_and_relateresult_construct() -> None:
    h = JoinHint(
        kind="shared_accession",
        resources=["geo:GSE1", "sra:SRP1"],
        key="PRJNA1",
        evidence="accession 'PRJNA1' present on 2 resources",
        suggestion="joinable on accession PRJNA1",
    )
    r = RelateResult(
        input_ids=["geo:GSE1", "sra:SRP1"], resolved=["geo:GSE1", "sra:SRP1"], hints=[h]
    )
    assert r.hints[0].kind == "shared_accession"
    assert r.errors == {}
    assert r.note is None


def test_shared_accession_collapses_to_one_hint() -> None:
    rs = [
        _res("geo:GSE1", accessions=["PRJNA1"]),
        _res("sra:SRP1", accessions=["prjna1"]),  # case-insensitive match
        _res("zenodo:9", accessions=["PRJNA1"]),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "shared_accession"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"geo:GSE1", "sra:SRP1", "zenodo:9"}
    assert hints[0].key == "PRJNA1"


def test_no_accession_hint_when_unshared() -> None:
    rs = [_res("geo:GSE1", accessions=["PRJNA1"]), _res("sra:SRP1", accessions=["PRJNA2"])]
    assert [h for h in relate_mod.detect(rs) if h.kind == "shared_accession"] == []


def test_shared_identifier_across_doi_and_identifiers() -> None:
    rs = [
        _res("pubmed:1", identifiers={"doi": "10.1/x", "pmid": "1"}),
        _res("zenodo:2", doi="10.1/X"),  # same doi, different case
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "shared_identifier"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"pubmed:1", "zenodo:2"}
    assert hints[0].key in ("10.1/x", "10.1/X")


def test_no_self_identifier_hint() -> None:
    # one resource carrying its own doi in both `doi` and `identifiers` must not self-hint
    rs = [_res("zenodo:2", doi="10.1/x", identifiers={"doi": "10.1/x"})]
    assert [h for h in relate_mod.detect(rs) if h.kind == "shared_identifier"] == []


def test_explicit_link_target_matches_another_resource() -> None:
    from data_aggregator_mcp.models import Link

    rs = [
        _res("pubmed:1", links=[Link(rel="describes", target_id="zenodo:2")]),
        _res("zenodo:2"),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "explicit_link"]
    assert len(hints) == 1
    assert set(hints[0].resources) == {"pubmed:1", "zenodo:2"}
    assert hints[0].key == "describes"


def test_explicit_link_to_outside_id_is_ignored() -> None:
    from data_aggregator_mcp.models import Link

    rs = [_res("pubmed:1", links=[Link(rel="describes", target_id="zenodo:999")]), _res("zenodo:2")]
    assert [h for h in relate_mod.detect(rs) if h.kind == "explicit_link"] == []


def test_version_lineage_directed_edge() -> None:
    rs = [
        _res("zenodo:1", superseded_by="zenodo:2"),  # 1 is older, points to newer 2
        _res("zenodo:2"),
    ]
    hints = [h for h in relate_mod.detect(rs) if h.kind == "version_lineage"]
    assert len(hints) == 1
    assert hints[0].resources == ["zenodo:2", "zenodo:1"]  # [newer, older]
    assert "newer version" in hints[0].suggestion


def test_no_hint_on_shared_organism_only() -> None:
    rs = [_res("zenodo:1", organism=["human"]), _res("zenodo:2", organism=["human"])]
    assert relate_mod.detect(rs) == []
