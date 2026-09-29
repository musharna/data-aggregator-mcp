from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import dataverse

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_DATASET = {
    "data": {
        "latestVersion": {
            "versionState": "RELEASED",
            "files": [
                {
                    "label": "language.py",
                    "restricted": False,
                    "dataFile": {
                        "id": 4202258,
                        "filename": "language.py",
                        "filesize": 590,
                        "md5": "d0763edaa9d9bd2a9516280e9044d885",
                    },
                },
                {
                    "label": "secret.csv",
                    "restricted": True,
                    "dataFile": {"id": 999, "filename": "secret.csv", "filesize": 10, "md5": "x"},
                },
            ],
        }
    }
}


async def test_files_parses_dataset_skips_restricted(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/TJCLKP",
        json=_DATASET,
    )
    async with httpx.AsyncClient() as client:
        files = await dataverse.files(client, "10.7910/DVN/TJCLKP")
    assert {f.name for f in files} == {"language.py"}  # restricted dropped
    f = files[0]
    assert f.url == "https://dataverse.harvard.edu/api/access/datafile/4202258"
    assert f.size == 590
    assert f.checksum == "md5:d0763edaa9d9bd2a9516280e9044d885"


async def test_base_url_env_override(httpx_mock, monkeypatch) -> None:
    monkeypatch.setenv("DATAVERSE_BASE_URL", "https://darus.uni-stuttgart.de")
    httpx_mock.add_response(
        url="https://darus.uni-stuttgart.de/api/datasets/:persistentId/?persistentId=doi:10.18419/X",
        json={"data": {"latestVersion": {"files": []}}},
    )
    async with httpx.AsyncClient() as client:
        files = await dataverse.files(client, "10.18419/X")
    assert files == []


def test_malformed_landing_url_is_treated_as_absent(monkeypatch, caplog) -> None:
    """Issue #85 class sweep: the landing URL comes from the DataCite record, and urlsplit
    raises a bare ValueError on an unclosed IPv6 bracket, which broke the file listing. A
    landing URL that does not parse gives no base URL: fall through to the DOI-prefix
    default, and say so in the log."""
    monkeypatch.delenv("DATAVERSE_BASE_URL", raising=False)
    assert dataverse._base_url("10.7910/DVN/X", "https://[::1/dataset.xhtml") == (
        dataverse.DEFAULT_BASE_URL
    )
    assert any("malformed landing URL" in m for m in caplog.messages)
    # positive control: a landing URL that parses supplies the installation's base URL
    assert (
        dataverse._base_url("10.7910/DVN/X", "https://dataverse.no/dataset.xhtml?x=1")
        == "https://dataverse.no"
    )


async def test_dataverse_malformed_body_raises_upstream(httpx_mock, monkeypatch) -> None:
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("data_aggregator_mcp._http.asyncio.sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/TJCLKP",
            text="<html>throttled</html>",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await dataverse.files(client, "10.7910/DVN/TJCLKP")


@live_only
async def test_live_dataverse_files_have_md5() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await dataverse.files(client, "10.7910/DVN/TJCLKP")
    assert files
    assert all(f.checksum and f.checksum.startswith("md5:") for f in files)


async def test_ingested_tabular_file_is_listed_as_the_original_its_md5_describes(
    httpx_mock,
) -> None:
    """A-H3 (audit 2026-09-27, shapes captured live from 10.7910/DVN/GSRD3R): Dataverse
    ingests an uploaded .xlsx into a derived .tab, but ``dataFile.md5`` stays the md5 of
    the ORIGINAL upload. Listing the .tab name/size/url with the original's md5 made
    every ingested file fail its checksum. Name, size, url and md5 must describe the
    same bytes: the original (``?format=original``)."""
    tab = {
        "label": "Soil properties.tab",
        "restricted": False,
        "dataFile": {
            "id": 3087285,
            "filename": "Soil properties.tab",
            "contentType": "text/tab-separated-values",
            "filesize": 1123,
            "originalFileFormat": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "originalFileSize": 19234,
            "originalFileName": "Soil properties.xlsx",
            "md5": "0935c3bdfa3a1048ac8dc4ca586b5c86",
            "tabularData": True,
        },
    }
    plain = {
        "label": "Dataset Description.docx",
        "restricted": False,
        "dataFile": {
            "id": 3087284,
            "filename": "Dataset Description.docx",
            "filesize": 20608,
            "md5": "38f01a6a36a2e8e376c959a1e1d4b74a",
            "tabularData": False,
        },
    }
    httpx_mock.add_response(
        url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/GSRD3R",
        json={"data": {"latestVersion": {"files": [tab, plain]}}},
    )
    async with httpx.AsyncClient() as client:
        files = await dataverse.files(client, "10.7910/DVN/GSRD3R")
    base = "https://dataverse.harvard.edu/api/access/datafile"
    assert [(f.name, f.size, f.url, f.checksum) for f in files] == [
        (
            "Soil properties.xlsx",
            19234,
            f"{base}/3087285?format=original",
            "md5:0935c3bdfa3a1048ac8dc4ca586b5c86",
        ),
        # positive control: a non-ingested file is listed exactly as before
        (
            "Dataset Description.docx",
            20608,
            f"{base}/3087284",
            "md5:38f01a6a36a2e8e376c959a1e1d4b74a",
        ),
    ]


@live_only
async def test_live_ingested_file_md5_matches_the_bytes_at_its_url() -> None:
    import hashlib

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        files = await dataverse.files(client, "10.7910/DVN/GSRD3R")
        f = next(f for f in files if f.name == "Soil properties.xlsx")
        body = (await client.get(f.url)).content
    assert len(body) == f.size
    assert f.checksum == f"md5:{hashlib.md5(body).hexdigest()}"  # nosec B324 - md5 is the upstream's checksum algorithm, compared not trusted
