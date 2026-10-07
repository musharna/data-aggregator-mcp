"""One hit per deposit: copies, versions and runs of one study are one search hit.

Round 2, page 1 of "snow leopard" (50 hits, 2026-10-07): 7 were SRA experiments of one
captive gut-virome study (SRP731052), 6 were second and third copies of three deposits
(a figshare ``.v1`` DOI and its unversioned twin; Zenodo versions of one concept, two
from DataCite and one from Zenodo itself), and 16 were Europe PMC's imports of papers
filed as BioStudies studies. Three keyed R4 studies waited just past it, and agents
search one page.
"""

from __future__ import annotations

import json
import os
import pathlib
import types

import httpx
import pytest

from data_aggregator_mcp import _mirror, router, zenodo
from data_aggregator_mcp.models import DataResource, Link

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
FX = pathlib.Path(__file__).parent / "fixtures"
CONCEPT = "10.5281/zenodo.15003684"


def _rec(id_: str, *, doi=None, links=(), kind="dataset", acc=(), source=None) -> DataResource:
    return DataResource(
        id=id_,
        source=source or id_.split(":", 1)[0],
        kind=kind,
        title=f"Snow leopard {id_}",
        doi=doi,
        links=[Link(rel=rel, target_id=t) for rel, t in links],
        accessions=list(acc),
    )


def test_copies_and_versions_share_a_key_and_distinct_deposits_do_not() -> None:
    key = _mirror.deposit_key
    # figshare's .v1 DOI is identical to its unversioned DOI.
    v1 = _rec(
        "datacite:f1",
        doi="10.6084/m9.figshare.c.1.v1",
        links=[("is_identical_to", "10.6084/m9.figshare.c.1")],
    )
    plain = _rec("datacite:f2", doi="10.6084/M9.FIGSHARE.C.1")
    assert key(v1) == key(plain) == "doi:10.6084/m9.figshare.c.1"
    # Every Zenodo version is a version of its concept, in any DOI spelling.
    a = _rec("datacite:z1", doi="10.5281/zenodo.16575233", links=[("is_version_of", CONCEPT)])
    b = _rec(
        "zenodo:2",
        doi="10.5281/zenodo.21887007",
        links=[("is_version_of", f"https://doi.org/{CONCEPT}")],
    )
    concept = _rec("datacite:z0", doi=CONCEPT)
    assert key(a) == key(b) == key(concept) == f"doi:{CONCEPT}"

    # Runs of one study are one deposit; runs of another study are another.
    def run(n: int, study: str) -> DataResource:
        return _rec(f"sra:SRX{n}", kind="sequencing_run", acc=[f"SRX{n}", study, "PRJNA9"])

    assert key(run(1, "SRP9")) == key(run(2, "SRP9")) == "study:SRP9"
    assert key(run(3, "ERP8")) == "study:ERP8"
    # Positive controls: a supplement link is a different object, not a copy; a paper's
    # accessions name no study; a record with nothing to key on stays its own hit.
    supplement = _rec("datacite:s", doi="10.6084/x", links=[("is_supplement_to", "10.1186/y")])
    assert key(supplement) == "doi:10.6084/x"
    assert key(_rec("x:1", kind="study", acc=["SRP9"])) is None
    assert key(_rec("x:2")) is None


def _serves(records: list[DataResource]):
    async def search(client, q, *, size=10, offset=0, **kw):
        return len(records), records[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


async def test_a_page_shows_each_deposit_once_and_its_copies_do_not_return(monkeypatch) -> None:
    zen = [
        _rec("zenodo:21887007", doi="10.5281/zenodo.21887007", links=[("is_version_of", CONCEPT)]),
        _rec("zenodo:other", doi="10.5281/zenodo.1"),
    ]
    dc = [
        _rec(
            "datacite:10.5281/zenodo.16575233",
            doi="10.5281/zenodo.16575233",
            links=[("is_version_of", CONCEPT)],
        ),
        _rec("datacite:fig.v1", doi="10.6084/fig.v1", links=[("is_identical_to", "10.6084/fig")]),
        _rec(
            "datacite:10.5281/zenodo.21638620",
            doi="10.5281/zenodo.21638620",
            links=[("is_version_of", CONCEPT)],
        ),
        _rec("datacite:fig", doi="10.6084/fig"),
    ]
    sra = [
        _rec(f"sra:SRX{n}", kind="sequencing_run", acc=[f"SRX{n}", "SRP731052", "PRJNA1519155"])
        for n in range(7)
    ] + [_rec("sra:SRX99", kind="sequencing_run", acc=["SRX99", "SRP42"])]
    adapters = {"zenodo": _serves(zen), "datacite": _serves(dc), "sra": _serves(sra)}
    monkeypatch.setattr(router, "_ADAPTERS", adapters)
    async with httpx.AsyncClient() as client:
        page = await router.search_page(
            client, query='"snow leopard"', size=20, sources=list(adapters)
        )
    assert sorted(r.id for r in page.results) == sorted(
        [
            "zenodo:21887007",  # the native copy wins its concept over DataCite's
            "zenodo:other",
            "datacite:fig.v1",
            "sra:SRX0",
            "sra:SRX99",  # positive control: a run of another study stays
        ]
    )
    # The copies were handled with their deposit: nothing is left to page to.
    assert page.next_cursor is None


def test_a_zenodo_version_links_its_concept() -> None:
    """A real record (fetched 2026-10-07): version 21887007 of concept 15003684, the
    concept two DataCite records of R4 S26 point at."""
    record = json.loads((FX / "zenodo_record_21887007.json").read_text())
    r = zenodo._normalize(record)
    assert Link(rel="is_version_of", target_id=CONCEPT) in r.links
    assert _mirror.deposit_key(r) == f"doi:{CONCEPT}"
    # Positive control: the concept record itself names no concept other than itself.
    own = {**record, "doi": CONCEPT}
    assert not any(lnk.rel == "is_version_of" for lnk in zenodo._normalize(own).links)


_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
# The R4 studies page 1 lacked (benchmarks/h2h/round2/keys/R4.yaml).
_WAITING = {
    "S28": ("22252314", "22252315", "22253310", "22253311", "33415759"),
    "S29": ("19184897",),
    "S30": ("16691668",),
    "S32": ("25736751",),
}


@_live
async def test_live_page_one_of_snow_leopard_holds_the_waiting_studies_once() -> None:
    """Probed 2026-10-07: page 1 (50) held none of S28-S32 before, all four after; the
    captive gut-virome study took 7 slots before, 1 after."""
    async with httpx.AsyncClient(timeout=90) as client:
        page = await router.search_page(client, query="snow leopard", size=50)
    blobs = [f"{r.id} {r.doi or ''}" for r in page.results]
    found = {k for k, ids in _WAITING.items() if any(i in b for b in blobs for i in ids)}
    assert len(found) >= 3, (found, blobs)
    virome = [r.id for r in page.results if "SRP731052" in r.accessions]
    assert len(virome) <= 1, virome
