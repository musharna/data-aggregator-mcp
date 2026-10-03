"""Each ENA filereport answer beside how the client must read it.

Probed live 2026-10-02 (www.ebi.ac.uk/ena/portal/api, anonymous):
- An accession ENA has no run for (unknown, or not mirrored) is ``200 []``. A malformed
  accession (``foo``, ``../x``, ``SRR390728?x=1``, empty) is ``400`` with a JSON
  ``message`` listing the accession patterns it accepts.
- A project expands to every run with no paging: PRJEB1787 to 249 runs, PRJEB37886 to
  2,700,701 (89 MB of run accessions alone, 38 s). ``omics.resolve`` only ever sends
  an experiment (SRX/ERX/DRX), which names a handful of runs.
- 25,514 runs from seven days of ``first_public`` between 2010 and 2025: every
  ``fastq_ftp``/``fastq_bytes``/``fastq_md5`` is a string, the three lists always have
  the same length, all 45,965 paths match ``ftp.sra.ebi.ac.uk/vol1/fastq/[A-Za-z0-9._/-]+``,
  every size is digits and every md5 32 lowercase hex digits. No list had an empty
  slot. 150 runs (0.6%) have no FASTQ, only ``submitted_ftp`` (a BAM, PacBio ``.bax.h5``),
  and their three FASTQ fields are ``""``. The check refuses 0 of the 25,514.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, ena
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


_V = "ftp.sra.ebi.ac.uk/vol1/fastq"

# Verbatim live answers (filereport, the client's own field list).
_SRX079566 = [
    {
        "run_accession": "SRR390728",
        "experiment_accession": "SRX079566",
        "study_accession": "PRJNA172563",
        "sample_accession": "SAMN00630374",
        "scientific_name": "Homo sapiens",
        "fastq_ftp": f"{_V}/SRR390/SRR390728/SRR390728_1.fastq.gz;"
        f"{_V}/SRR390/SRR390728/SRR390728_2.fastq.gz",
        "fastq_bytes": "101304405;101858469",
        "fastq_md5": "a04ed1f37028cda9486eec9726237796;3d440434c968624acaa4f787d48efe2f",
    },
    {
        "run_accession": "SRR292241",
        "experiment_accession": "SRX079566",
        "study_accession": "PRJNA172563",
        "sample_accession": "SAMN00630374",
        "scientific_name": "Homo sapiens",
        "fastq_ftp": f"{_V}/SRR292/SRR292241/SRR292241_1.fastq.gz;"
        f"{_V}/SRR292/SRR292241/SRR292241_2.fastq.gz",
        "fastq_bytes": "387227151;395115704",
        "fastq_md5": "a5e0d2d51550127ea9ce3a0219deb375;e9ce7abd3bce9d5ff194d6e045a36c1c",
    },
]
# A single-file (unpaired) run.
_SRX1451647 = [
    {
        "run_accession": "SRR2959864",
        "experiment_accession": "SRX1451647",
        "study_accession": "PRJNA298336",
        "sample_accession": "SAMN04287983",
        "scientific_name": "human gut metagenome",
        "fastq_ftp": f"{_V}/SRR295/004/SRR2959864/SRR2959864.fastq.gz",
        "fastq_bytes": "126",
        "fastq_md5": "36c7a01636fbf7b52fbdba22d5998db2",
    }
]
# A run ENA holds only as the submitted BAM: no FASTQ.
_SRX27395936 = [
    {
        "run_accession": "SRR32046810",
        "experiment_accession": "SRX27395936",
        "study_accession": "PRJNA1212730",
        "sample_accession": "SAMN46306874",
        "scientific_name": "Mus musculus",
        "fastq_ftp": "",
        "fastq_bytes": "",
        "fastq_md5": "",
    }
]
_RUN = _SRX079566[0]


def _entry(path: str, size: int | None, md5: str | None) -> FileEntry:
    return FileEntry(
        name=path.rpartition("/")[2],
        size=size,
        url="https://" + path,
        checksum=None if md5 is None else "md5:" + md5,
    )


async def _report(body: object, accession: str = "SRX079566") -> list[FileEntry]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
    ) as client:
        return await ena.filereport(client, accession)


@pytest.mark.asyncio
async def test_each_real_answer_is_read_file_by_file():
    assert await _report(_SRX079566) == [
        FileEntry(
            name="SRR390728_1.fastq.gz",
            size=101304405,
            url=f"https://{_V}/SRR390/SRR390728/SRR390728_1.fastq.gz",
            checksum="md5:a04ed1f37028cda9486eec9726237796",
        ),
        FileEntry(
            name="SRR390728_2.fastq.gz",
            size=101858469,
            url=f"https://{_V}/SRR390/SRR390728/SRR390728_2.fastq.gz",
            checksum="md5:3d440434c968624acaa4f787d48efe2f",
        ),
        FileEntry(
            name="SRR292241_1.fastq.gz",
            size=387227151,
            url=f"https://{_V}/SRR292/SRR292241/SRR292241_1.fastq.gz",
            checksum="md5:a5e0d2d51550127ea9ce3a0219deb375",
        ),
        FileEntry(
            name="SRR292241_2.fastq.gz",
            size=395115704,
            url=f"https://{_V}/SRR292/SRR292241/SRR292241_2.fastq.gz",
            checksum="md5:e9ce7abd3bce9d5ff194d6e045a36c1c",
        ),
    ]
    assert await _report(_SRX1451647) == [
        FileEntry(
            name="SRR2959864.fastq.gz",
            size=126,
            url=f"https://{_V}/SRR295/004/SRR2959864/SRR2959864.fastq.gz",
            checksum="md5:36c7a01636fbf7b52fbdba22d5998db2",
        )
    ]


@pytest.mark.asyncio
async def test_no_run_and_a_run_without_fastq_list_nothing_beside_a_run_that_has_some():
    assert await _report([]) == []
    assert await _report(_SRX27395936) == []
    # Positive control: in one answer, the FASTQ-less run adds nothing and the other
    # run is read whole.
    assert await _report(_SRX27395936 + _SRX1451647) == await _report(_SRX1451647)
    assert len(await _report(_SRX1451647)) == 1


@pytest.mark.asyncio
async def test_an_empty_slot_in_equal_length_lists_is_read_as_absent_at_its_own_index():
    a, b, c = (f"{_V}/X/{n}.fastq.gz" for n in "abc")
    m1, m3 = "1" * 32, "3" * 32
    rec = {"fastq_ftp": f"{a};{b};{c}", "fastq_bytes": "10;;30", "fastq_md5": f"{m1};;{m3}"}
    assert await _report([rec]) == [_entry(a, 10, m1), _entry(b, None, None), _entry(c, 30, m3)]
    # A missing path slot drops that slot only; the files after it keep their own
    # size and md5.
    rec = {"fastq_ftp": f"{a};;{c}", "fastq_bytes": "10;20;30", "fastq_md5": f"{m1};;{m3}"}
    assert await _report([rec]) == [_entry(a, 10, m1), _entry(c, 30, m3)]


def _with(field: str, value: object) -> list[dict]:
    rec = copy.deepcopy(_RUN)
    rec[field] = value
    return [rec, copy.deepcopy(_SRX079566[1])]


_ONE, _TWO = _RUN["fastq_ftp"].split(";")
_MD5_A, _MD5_B = _RUN["fastq_md5"].split(";")

_MALFORMED = {
    "not a list of records": ["SRR390728"],
    "a null record": [None],
    "a list record": [[_RUN]],
    "fastq_ftp missing": [{k: v for k, v in _RUN.items() if k != "fastq_ftp"}],
    "fastq_bytes missing": [{k: v for k, v in _RUN.items() if k != "fastq_bytes"}],
    "fastq_md5 missing": [{k: v for k, v in _RUN.items() if k != "fastq_md5"}],
    "fastq_ftp null": _with("fastq_ftp", None),
    "fastq_bytes a number": _with("fastq_bytes", 101304405),
    "fastq_md5 a list": _with("fastq_md5", [_MD5_A, _MD5_B]),
    # Ragged lists: which file a size or md5 belongs to is unknowable.
    "one size for two files": _with("fastq_bytes", "101304405"),
    "one md5 for two files": _with("fastq_md5", _MD5_B),
    "no md5 for two files": _with("fastq_md5", ""),
    "three sizes for two files": _with("fastq_bytes", "1;2;3"),
    # Values the reader would turn into something else.
    "a signed size": _with("fastq_bytes", "-1;101858469"),
    "a size in exponent form": _with("fastq_bytes", "1e3;101858469"),
    "a non-ASCII digit size": _with("fastq_bytes", "²;101858469"),
    "an upper-case md5": _with("fastq_md5", f"{_MD5_A.upper()};{_MD5_B}"),
    "a short md5": _with("fastq_md5", f"{_MD5_A[:-1]};{_MD5_B}"),
    "a long md5": _with("fastq_md5", f"{_MD5_A}0;{_MD5_B}"),
    # Paths that would become a URL off ftp.sra.ebi.ac.uk, or a name that is not a file.
    "an https URL": _with("fastq_ftp", f"https://{_ONE};{_TWO}"),
    "an ftp URL": _with("fastq_ftp", f"ftp://{_ONE};{_TWO}"),
    "another host": _with("fastq_ftp", f"evil.example/x/SRR390728_1.fastq.gz;{_TWO}"),
    "a look-alike host": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk.evil.example/x.gz;{_TWO}"),
    "userinfo": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk@evil.example/x.gz;{_TWO}"),
    "a port": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk:8443/vol1/x.gz;{_TWO}"),
    "a backslash": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk\\@evil.example/x.gz;{_TWO}"),
    "a dot-dot segment": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk/vol1/../../x.gz;{_TWO}"),
    "a dot segment": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk/./x.gz;{_TWO}"),
    "an empty segment": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk//x.gz;{_TWO}"),
    "a directory": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk/vol1/fastq/;{_TWO}"),
    "the bare host": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk;{_TWO}"),
    "a query": _with("fastq_ftp", f"{_ONE}?x=1;{_TWO}"),
    "a fragment": _with("fastq_ftp", f"{_ONE}#x;{_TWO}"),
    "an escape": _with("fastq_ftp", f"ftp.sra.ebi.ac.uk/%2e%2e/x.gz;{_TWO}"),
    "a space": _with("fastq_ftp", f"{_ONE} ;{_TWO}"),
    "a trailing newline": _with("fastq_ftp", f"{_ONE};{_TWO}\n"),
    "a non-ASCII letter": _with("fastq_ftp", f"{_ONE[:-3]}ɡz;{_TWO}"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", list(_MALFORMED.values()), ids=list(_MALFORMED))
async def test_a_malformed_answer_is_an_outage_not_a_manifest(body):
    """Each body breaks the contract every live run keeps; it must be refused (retried,
    then raised), never read as a shorter, misaligned or off-host manifest."""
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] ENA filereport returned an unparseable 200 body "
        r"after 3 tries: UpstreamEnvelopeError\(.(no ENA filereport run in|ENA filereport run )",
    ):
        await _report(body)
    # Positive control: the same answer, unbroken, is read.
    assert len(await _report(_SRX079566)) == 4


_WRONG = ["x", "", 7, True, 1.5, [1], {"k": 1}, None]


def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """Every field of a real record with every JSON type: each must be refused by the
    check or read cleanly, so a field the reader starts using without the check fails
    here (the datacite lesson of 2026-10-01)."""
    ena._check_report(_SRX079566)  # positive control: the real answer passes, read whole
    assert len(ena._entries_from_record(_RUN)) == 2
    escaped = []
    for field in _RUN:
        for value in _WRONG:
            body = _with(field, value)
            try:
                ena._check_report(body)
            except _http.UpstreamEnvelopeError:
                continue
            try:
                for rec in body:
                    ena._entries_from_record(rec)
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((field, value, type(exc).__name__))
    assert escaped == []


def test_the_check_refuses_with_a_message_naming_the_run_and_the_field():
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no ENA filereport run in 'x'$"):
        ena._check_report(["x"])
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^ENA filereport run 'SRR390728' has a malformed fastq_bytes: 101304405$",
    ):
        ena._check_report(_with("fastq_bytes", 101304405))
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^ENA filereport run 'SRR390728' has a malformed fastq_md5: None$",
    ):
        ena._check_report(_with("fastq_md5", None))
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^ENA filereport run None has a malformed fastq_ftp: 'evil.example/x'$",
    ):
        ena._check_report([{"fastq_ftp": "evil.example/x"}])
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^ENA filereport run 'SRR390728' lists 2 files, 1 sizes and 2 md5s$",
    ):
        ena._check_report(_with("fastq_bytes", "7"))
    with pytest.raises(
        _http.UpstreamEnvelopeError,
        match=r"^ENA filereport run 'SRR390728' lists 2 files, 2 sizes and 1 md5s$",
    ):
        ena._check_report(_with("fastq_md5", ""))
    # Quoted values are cut at 200 characters (40 for the run), not quoted whole.
    rec = {"run_accession": "R" * 100, "fastq_ftp": "x" * 500}
    with pytest.raises(_http.UpstreamEnvelopeError) as err:
        ena._check_report([rec])
    assert str(err.value) == (
        f"ENA filereport run {repr('R' * 100)[:40]} has a malformed fastq_ftp: "
        + repr("x" * 500)[:200]
    )
    with pytest.raises(_http.UpstreamEnvelopeError) as err:
        ena._check_report([["y" * 500]])
    assert str(err.value) == "no ENA filereport run in " + repr(["y" * 500])[:200]
    ena._check_report(_SRX079566 + _SRX27395936)  # positive control
