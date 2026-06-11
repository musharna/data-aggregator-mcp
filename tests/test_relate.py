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
