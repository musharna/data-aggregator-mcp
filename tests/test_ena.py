from __future__ import annotations

import httpx
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import ena

_FIELDS = (
    "run_accession,experiment_accession,study_accession,sample_accession,"
    "scientific_name,fastq_ftp,fastq_bytes,fastq_md5"
)


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
                "fastq_md5": "aaa;bbb",
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        files = await ena.filereport(client, "SRX1")
    assert len(files) == 2
    assert files[0].name == "SRR1_1.fastq.gz"
    assert files[0].url == "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_1.fastq.gz"
    assert files[0].size == 100
    assert files[0].checksum == "md5:aaa"
    assert files[1].name == "SRR1_2.fastq.gz"
    assert files[1].size == 200
    assert files[1].checksum == "md5:bbb"


async def test_filereport_empty_when_not_mirrored(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_url("SRRX"), json=[])
    async with httpx.AsyncClient() as client:
        assert await ena.filereport(client, "SRRX") == []


async def test_filereport_tolerates_ragged_size_and_md5_lists(httpx_mock: HTTPXMock) -> None:
    # two FASTQ paths but only one byte count and no md5 — must not IndexError
    httpx_mock.add_response(
        url=_url("SRX2"),
        json=[
            {
                "run_accession": "SRR2",
                "fastq_ftp": "ftp.sra.ebi.ac.uk/x/a.fastq.gz;ftp.sra.ebi.ac.uk/x/b.fastq.gz",
                "fastq_bytes": "500",
                "fastq_md5": "",
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        files = await ena.filereport(client, "SRX2")
    assert len(files) == 2
    assert files[0].size == 500
    assert files[0].checksum is None
    assert files[1].size is None
    assert files[1].checksum is None
