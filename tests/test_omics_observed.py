"""What the omics adapter asks NCBI for and what it says back, pinned exactly.

Each test stubs ``_eutils`` with a recorder, so the arguments the adapter passes (db,
term, page size, offset) and the messages it raises are compared whole, not by
substring. The record shapes come from ``test_omics_answers.py`` (live 2026-10-02).
"""

from __future__ import annotations

import logging

import pytest

from data_aggregator_mcp import omics
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from tests.test_omics_answers import _BIOPROJECT, _GDS, _SRA


class _NCBI:
    """Records every esearch/esummary/elink call; answers from ``docs[db]``."""

    def __init__(self, monkeypatch, docs: dict[str, list[dict]], *, count: int | None = None):
        self.docs = docs
        self.count = count
        self.esearch: list[tuple[str, str, int, int]] = []
        self.esummary: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(omics._eutils, "esearch", self._esearch)
        monkeypatch.setattr(omics._eutils, "esummary", self._esummary)

    async def _esearch(self, client, db, term, *, retmax, retstart=0):
        self.esearch.append((db, term, retmax, retstart))
        uids = [d["uid"] for d in self.docs.get(db, [])][:retmax]
        return (len(uids) if self.count is None else self.count), uids

    async def _esummary(self, client, db, ids):
        self.esummary.append((db, ids))
        by_uid = {d["uid"]: d for d in self.docs[db]}
        return [by_uid[u] for u in ids]


def test_year_is_the_leading_four_digits_or_none() -> None:
    assert omics._year_from("2026") == 2026
    assert omics._year_from("2026/10/01 00:00") == 2026
    for text in (None, "", "20", "202", "abcd", "x2026"):
        assert omics._year_from(text) is None, text


def test_each_live_summary_becomes_this_exact_record() -> None:
    """Whole records, every field, from the live summaries."""
    geo = omics._normalize("gds", _GDS).model_dump(exclude_defaults=True)
    assert geo == {
        "id": "geo:GDS6063",
        "source": "geo",
        "kind": "study",
        "title": "Influenza A effect on plasmacytoid dendritic cells",
        "year": 2016,
        "description": _GDS["summary"],
        "accessions": ["GDS6063"],
        "organism": ["Homo sapiens"],
    }
    sra = omics._normalize("sra", _SRA).model_dump(exclude_defaults=True)
    assert sra == {
        "id": "sra:SRX35511800",
        "source": "sra",
        "kind": "sequencing_run",
        "title": "Rhizosphere microbiome in cultivated Houttuynia cordata",
        "year": 2026,
        "description": "16S rRNA gene amplicon sequencing of Houttuynia cordata rhizosphere: "
        "Broth control replicate 5",
        "accessions": ["SRX35511800", "SRP741891", "PRJNA1538143", "SRR40983380"],
        "organism": ["rhizosphere metagenome"],
    }
    bp = omics._normalize("bioproject", {**_BIOPROJECT, "organism_name": "Citrus"})
    assert bp.model_dump(exclude_defaults=True) == {
        "id": "bioproject:PRJNA1537786",
        "source": "bioproject",
        "kind": "study",
        "title": _BIOPROJECT["project_title"],
        "year": 2026,
        "description": _BIOPROJECT["project_description"],
        "accessions": ["PRJNA1537786"],
        "organism": ["Citrus"],
    }


def test_an_sra_experiment_without_a_study_takes_its_summary_title_once() -> None:
    """With no study name the experiment's Summary/Title is the title, and is not
    repeated as the description. Positive control: with a study name it moves to the
    description."""
    alone = {"expxml": '<Summary><Title>x</Title></Summary><Experiment acc="SRX1"/>'}
    r = omics._normalize("sra", alone)
    assert (r.title, r.description) == ("x", None)
    named = {"expxml": alone["expxml"] + '<Study acc="SRP1" name="s"/>'}
    r = omics._normalize("sra", named)
    assert (r.title, r.description) == ("s", "x")


async def test_a_subsource_search_asks_one_db_for_one_page(monkeypatch) -> None:
    """``search_subsource`` asks its own db, with the page size capped at ``MAX_SIZE``
    and no offset unless one is given."""
    ncbi = _NCBI(monkeypatch, {"gds": [_GDS]})
    total, recs = await omics.search_subsource(None, "geo", "influenza")
    assert ncbi.esearch == [("gds", "influenza", omics.DEFAULT_SIZE, 0)]
    assert ncbi.esummary == [("gds", ["6063"])]
    assert (total, [r.id for r in recs]) == (1, ["geo:GDS6063"])
    await omics.search_subsource(None, "bioproject", "q", size=500, offset=30)
    assert ncbi.esearch[-1] == ("bioproject", "q", omics.MAX_SIZE, 30)


async def test_search_passes_its_page_size_to_every_db(monkeypatch) -> None:
    ncbi = _NCBI(monkeypatch, {"gds": [_GDS], "sra": [_SRA], "bioproject": [_BIOPROJECT]})
    total, recs = await omics.search(None, "q", size=5)
    assert sorted(ncbi.esearch) == [
        ("bioproject", "q", 5, 0),
        ("gds", "q", 5, 0),
        ("sra", "q", 5, 0),
    ]
    assert total == 3
    assert [r.id for r in recs] == ["geo:GDS6063", "sra:SRX35511800", "bioproject:PRJNA1537786"]
    await omics.search(None, "q", size=500, offset=7)
    assert {call[2:] for call in ncbi.esearch[3:]} == {(omics.MAX_SIZE, 7)}
    _total, two = await omics.search(None, "q", size=2)
    assert [r.id for r in two] == ["geo:GDS6063", "sra:SRX35511800"]


async def test_search_names_every_db_when_all_fail(monkeypatch) -> None:
    """A total outage is one error naming each db; one db failing is tolerated."""

    async def down(client, db, term, *, retmax, retstart=0):
        if db == "sra" and term == "partial":
            return 0, []
        raise UpstreamUnavailableError(f"{db} down")

    monkeypatch.setattr(omics._eutils, "esearch", down)
    assert await omics.search(None, "partial") == (0, [])
    with pytest.raises(UpstreamUnavailableError) as exc:
        await omics.search(None, "q")
    err = "UpstreamUnavailableError: [UpstreamUnavailableError]"
    assert str(exc.value) == (
        "[UpstreamUnavailableError] omics search: every backend failed ("
        f"geo: {err} gds down; sra: {err} sra down; bioproject: {err} bioproject down)"
    )


async def test_resolve_reads_the_prefix_up_to_the_first_colon(monkeypatch) -> None:
    ncbi = _NCBI(monkeypatch, {"gds": [], "sra": []})
    with pytest.raises(
        NotFoundError, match=r"^\[NotFoundError\] no omics record for 'GSE1:x' in geo$"
    ):
        await omics.resolve(None, "geo:GSE1:x")
    assert ncbi.esearch == [("gds", "GSE1:x[ACCN] AND gse[ETYP]", 20, 0)]


async def test_resolve_adds_a_geo_entry_type_only_for_geo_accessions(monkeypatch) -> None:
    """``[ETYP]`` is GEO's field and names only GSE/GSM/GPL/GDS. Positive control: a
    GEO accession gets it."""
    ncbi = _NCBI(monkeypatch, {"gds": [], "sra": []})
    for rid in ("sra:GSM1", "geo:XYZ1", "geo:GSM2"):
        with pytest.raises(NotFoundError):
            await omics.resolve(None, rid)
    assert [term for _db, term, _n, _o in ncbi.esearch] == [
        "GSM1[ACCN]",
        "XYZ1[ACCN]",
        "GSM2[ACCN] AND gsm[ETYP]",
    ]


def _experiment(uid: str, srx: str) -> dict:
    return {"uid": uid, "expxml": f'<Experiment acc="{srx}"/><Study acc="SRP1" name="s"/>'}


async def test_resolve_names_the_records_that_carry_a_study_exactly(monkeypatch) -> None:
    """The message lists every carrier, and says how many NCBI matched only when that
    is more than it read."""
    docs = {"sra": [_experiment("1", "SRX1"), _experiment("2", "SRX2")]}
    tail = " — resolve one of those ids"
    head = "[NotFoundError] 'sra:SRP1' is not itself a sra record; it belongs to sra:SRX1, sra:SRX2"
    _NCBI(monkeypatch, docs)
    with pytest.raises(NotFoundError) as exc:
        await omics.resolve(None, "sra:SRP1")
    assert str(exc.value) == head + tail
    _NCBI(monkeypatch, docs, count=40)
    with pytest.raises(NotFoundError) as exc:
        await omics.resolve(None, "sra:SRP1")
    assert str(exc.value) == head + " (first 2 of 40)" + tail


async def _links(monkeypatch, n: int, caplog) -> tuple[list[str], str | None, list[list[str]]]:
    asked: list[list[str]] = []

    async def fake_elink(client, *, dbfrom, db, ids):
        assert (dbfrom, db, ids) == ("bioproject", "sra", ["231221"])
        return [str(i) for i in range(n)]

    async def fake_esummary(client, db, ids):
        asked.append(ids)
        return [{"uid": u, "expxml": f'<Experiment acc="SRX{u}"/>'} for u in ids]

    monkeypatch.setattr(omics._eutils, "elink", fake_elink)
    monkeypatch.setattr(omics._eutils, "esummary", fake_esummary)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=omics.logger.name):
        links, note = await omics._bioproject_sra_links(None, "231221")
    return [lnk.target_id for lnk in links], note, asked


async def test_bioproject_links_are_capped_with_this_exact_note(monkeypatch, caplog) -> None:
    """At the cap every run is linked and nothing is said; one past it, the first
    ``MAX_LINKED_RUNS`` are linked and the note and log say so in these words."""
    monkeypatch.setattr(omics, "MAX_LINKED_RUNS", 3)
    ids, note, asked = await _links(monkeypatch, 3, caplog)
    assert (ids, note, asked, caplog.records) == (
        ["sra:SRX0", "sra:SRX1", "sra:SRX2"],
        None,
        [["0", "1", "2"]],
        [],
    )
    ids, note, asked = await _links(monkeypatch, 4, caplog)
    assert (ids, asked) == (["sra:SRX0", "sra:SRX1", "sra:SRX2"], [["0", "1", "2"]])
    assert note == (
        "first 3 of 4 SRA runs; search sources=['omics'] for the project accession to page them all"
    )
    assert [r.getMessage() for r in caplog.records] == [
        "BioProject uid=231221 links 4 SRA runs; attaching the first 3 "
        "(search sources=['omics'] for the project accession to page them all)"
    ]
    ids, note, asked = await _links(monkeypatch, 0, caplog)
    assert (ids, note, asked) == ([], None, [])
