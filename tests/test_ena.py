from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import _http, ena
from data_aggregator_mcp.errors import UpstreamUnavailableError

_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,"
    "scientific_name,fastq_ftp,fastq_bytes,fastq_md5"
)
_MD5_1, _MD5_2 = "a" * 32, "b" * 32


def _url(acc: str) -> str:
    return (
        "https://www.ebi.ac.uk/ena/portal/api/filereport"
        f"?accession={acc}&result=read_run&fields={_FIELDS}&format=json"
    )


async def test_filereport_splits_paired_fastq_into_file_entries(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_url("SRX1"),
        json=[
            {
                "run_accession": "SRR1",
                "fastq_ftp": "ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_1.fastq.gz;ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_2.fastq.gz",
                "fastq_bytes": "100;200",
                "fastq_md5": f"{_MD5_1};{_MD5_2}",
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        files = await ena.filereport(client, "SRX1")
    assert len(files) == 2
    assert files[0].name == "SRR1_1.fastq.gz"
    assert files[0].url == "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_1.fastq.gz"
    assert files[0].size == 100
    assert files[0].checksum == f"md5:{_MD5_1}"
    assert files[1].name == "SRR1_2.fastq.gz"
    assert files[1].size == 200
    assert files[1].checksum == f"md5:{_MD5_2}"


async def test_filereport_empty_when_not_mirrored(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_url("SRRX"), json=[])
    async with httpx.AsyncClient() as client:
        assert await ena.filereport(client, "SRRX") == []


async def test_filereport_refuses_ragged_size_and_md5_lists(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """Two FASTQ paths but one byte count and no md5: which file the count belongs to is
    unknowable, so the answer is malformed. It used to give the first file the count
    and both files no checksum, which pairs a size or md5 with the wrong file whenever
    the missing one is not the last. No live run has ragged lists (0 of 25,514)."""

    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    two = "ftp.sra.ebi.ac.uk/x/a.fastq.gz;ftp.sra.ebi.ac.uk/x/b.fastq.gz"
    for _ in range(3):
        httpx_mock.add_response(
            url=_url("SRX2"),
            json=[
                {"run_accession": "SRR2", "fastq_ftp": two, "fastq_bytes": "500", "fastq_md5": ""}
            ],
        )
    httpx_mock.add_response(
        url=_url("SRX3"),
        json=[{"run_accession": "SRR3", "fastq_ftp": two, "fastq_bytes": ";500", "fastq_md5": ";"}],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"ENA filereport run 'SRR2' lists 2 files, 1 sizes and 1 md5s",
        ):
            await ena.filereport(client, "SRX2")
        # Positive control: the same paths with equal-length lists are read slot by slot,
        # so the one count goes to the file it is listed beside.
        files = await ena.filereport(client, "SRX3")
    assert [(f.name, f.size, f.checksum) for f in files] == [
        ("a.fastq.gz", None, None),
        ("b.fastq.gz", 500, None),
    ]


LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# ERX3243703 is one paired-end run (ERR3216176) whose two FASTQ files are 176 and 197
# bytes, so the whole manifest can be downloaded and hashed in well under a second.
_TINY_PAIRED = "ERX3243703"


@live_only
async def test_live_each_listed_url_serves_the_listed_size_and_md5() -> None:
    """The pairing a file-to-checksum misalignment would break: each URL's bytes must
    have that file's own size and md5, for both files of a pair."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        files = await ena.filereport(client, _TINY_PAIRED)
        assert [f.name for f in files] == ["ERR3216176_1.fastq.gz", "ERR3216176_2.fastq.gz"]
        for f in files:
            assert f.url and f.url.startswith("https://ftp.sra.ebi.ac.uk/vol1/fastq/")
            body = (await client.get(f.url)).raise_for_status().content
            assert (len(body), f"md5:{hashlib.md5(body).hexdigest()}") == (f.size, f.checksum)


@live_only
async def test_live_no_run_and_no_fastq_are_empty_and_a_bad_accession_is_an_error() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        # An unknown accession: ENA answers 200 [].
        assert await ena.filereport(client, "SRR99999999999") == []
        # A run ENA holds only as its submitted BAM (SRR32046810): no FASTQ to list.
        assert await ena.filereport(client, "SRX27395936") == []
        # A malformed accession: ENA answers 400, which is an error, not "no files".
        with pytest.raises(UpstreamUnavailableError, match=r"ENA filereport → HTTP 400: "):
            await ena.filereport(client, "../x")
        # Positive control: a mirrored experiment lists its FASTQ.
        assert len(await ena.filereport(client, _TINY_PAIRED)) == 2
