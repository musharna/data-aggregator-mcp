from __future__ import annotations

from data_aggregator_mcp.models import JoinHint, RelateResult


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
