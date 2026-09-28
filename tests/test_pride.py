import os

import httpx
import pytest

from data_aggregator_mcp import pride

_FILES = [
    {
        "fileName": "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        "fileSizeBytes": 497985,
        "checksum": "",
        "publicFileLocations": [
            {
                "name": "FTP Protocol",
                "value": "ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
            },
            {
                "name": "Aspera Protocol",
                "value": "prd_ascp@fasp.ebi.ac.uk:pride/data/archive/2012/03/PXD000001/...",
            },
        ],
    },
    {"fileName": "no_public_loc.raw", "fileSizeBytes": 10, "publicFileLocations": []},
]


@pytest.mark.asyncio
async def test_files_rewrites_ftp_to_https_and_keeps_size():
    async def handler(request):
        if request.url.path.endswith("/projects/PXD000001/files/count"):
            return httpx.Response(200, json=len(_FILES))
        assert request.url.path.endswith("/projects/PXD000001/files")
        return httpx.Response(200, json=_FILES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await pride.files(c, "PXD000001")
    assert len(files) == 1  # the no-public-location entry is dropped
    f = files[0]
    assert f.url == (
        "https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/"
        "PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz"
    )
    assert f.size == 497985
    assert f.checksum is None  # PRIDE exposes no usable checksum
    assert f.source == "pride"


def _project(n):
    return [
        {
            "accession": f"acc{i:04d}",
            "fileName": f"run_{i:04d}.raw",
            "fileSizeBytes": i,
            "publicFileLocations": [
                {
                    "name": "FTP Protocol",
                    "value": f"ftp://ftp.pride.ebi.ac.uk/pride/x/run_{i:04d}.raw",
                }
            ],
        }
        for i in range(n)
    ]


def _paged_server(entries, count=None, pages_served=None):
    """Serve like PRIDE v3: /files/count is the total, /files is 0-indexed pages whose
    pageSize is capped at 100 (a larger request still returns 100)."""

    def handler(request):
        if request.url.path.endswith("/files/count"):
            return httpx.Response(200, json=len(entries) if count is None else count)
        size = min(int(request.url.params.get("pageSize", 100)), 100)
        page = int(request.url.params.get("page", 0))
        if pages_served is not None:
            pages_served.append(page)
        return httpx.Response(200, json=entries[page * size : (page + 1) * size])

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_files_pages_to_the_total():
    """B-H2: one unpaged request returned PRIDE's first 100 files, so PXD002179 listed
    100 of 306. Every page is read and the union is the whole project."""
    entries = _project(306)
    served: list[int] = []
    async with httpx.AsyncClient(transport=_paged_server(entries, pages_served=served)) as c:
        files = await pride.files(c, "PXD002179")
    assert [f.name for f in files] == [e["fileName"] for e in entries]
    assert sorted(served) == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_files_short_of_count_raises():
    """Pages that run out before /files/count is reached are a truncated manifest: raise
    naming PRIDE and the accession. Positive control: a one-page project whose count
    matches returns exactly its files."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async with httpx.AsyncClient(transport=_paged_server(_project(7))) as c:
        assert len(await pride.files(c, "PXD000007")) == 7
    async with httpx.AsyncClient(transport=_paged_server(_project(150), count=306)) as c:
        with pytest.raises(UpstreamUnavailableError, match=r"PRIDE.*PXD002179.*150 of 306"):
            await pride.files(c, "PXD002179")


@pytest.mark.asyncio
async def test_files_page_guard_raises(monkeypatch):
    """A count needing more than _MAX_PAGES pages raises before paging, never a silent
    cap. Positive control: a count that fits under the same guard is paged in full."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    monkeypatch.setattr(pride, "_MAX_PAGES", 2)
    async with httpx.AsyncClient(transport=_paged_server(_project(200))) as c:
        assert len(await pride.files(c, "PXD000200")) == 200
    served: list[int] = []
    async with httpx.AsyncClient(transport=_paged_server(_project(201), pages_served=served)) as c:
        with pytest.raises(UpstreamUnavailableError, match=r"PRIDE.*PXD000201.*201 files"):
            await pride.files(c, "PXD000201")
    assert served == []


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_pride_files_https_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await pride.files(c, "PXD000001")
        assert files, "PXD000001 should list files"
        head = await c.head(files[0].url)
        assert head.status_code < 400  # rewritten HTTPS url serves


@_live_only
@pytest.mark.asyncio
async def test_live_pride_manifest_is_complete():
    """PXD002179 has 306 files (4 pages at PRIDE's 100-per-page ceiling)."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await pride.files(c, "PXD002179")
    assert len(files) == len({f.url for f in files}) == 306
