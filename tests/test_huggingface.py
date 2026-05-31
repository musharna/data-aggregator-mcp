import httpx
import pytest

from data_aggregator_mcp import huggingface
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator

_DS = {
    "id": "owner/name",
    "author": "owner",
    "createdAt": "2022-06-09T17:34:13.000Z",
    "tags": ["license:mit", "format:parquet", "biology"],
    "gated": False,
}


@pytest.mark.asyncio
async def test_search_normalizes():
    async def handler(request):
        assert request.url.params["search"] == "dna"
        return httpx.Response(200, json=[_DS])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await huggingface.search(c, "dna", size=10)
    assert total == 1
    r = recs[0]
    assert r.id == "hf:owner/name" and r.source == "huggingface" and r.kind == "dataset"
    assert r.creators == [Creator(name="owner")]
    assert r.year == 2022 and r.license == "mit" and r.access == "open"


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[_DS]))
    ) as c:
        total, recs = await huggingface.search(c, "dna", size=10, offset=5)
    assert (total, recs) == (0, [])


@pytest.mark.asyncio
async def test_resolve_attaches_files_skips_gitattributes():
    body = {
        **_DS,
        "siblings": [
            {"rfilename": ".gitattributes"},
            {"rfilename": "data/train.parquet"},
        ],
    }

    async def handler(request):
        assert request.url.path.endswith("/api/datasets/owner/name")
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await huggingface.resolve(c, "hf:owner/name")
    names = [f.name for f in r.files]
    assert names == ["data/train.parquet"]
    assert (
        r.files[0].url
        == "https://huggingface.co/datasets/owner/name/resolve/main/data/train.parquet"
    )


@pytest.mark.asyncio
async def test_resolve_404():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(NotFoundError):
            await huggingface.resolve(c, "hf:owner/missing")


def test_normalize_no_license_tag():
    assert huggingface._normalize({"id": "a/b", "author": "a", "tags": []}).license is None


@pytest.mark.asyncio
async def test_search_gated_is_restricted():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{**_DS, "gated": True}]))
    ) as c:
        _t, recs = await huggingface.search(c, "x", size=5)
    assert recs[0].access == "restricted"


import os as _os

_LIVE = _os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_and_resolve() -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        total, recs = await huggingface.search(client, "dna", size=5)
        assert total >= 1
        r0 = recs[0]
        assert r0.id.startswith("hf:") and r0.source == "huggingface" and r0.kind == "dataset"
        # resolve a known small dataset → files attached with working URLs
        full = await huggingface.resolve(client, "hf:davidcechak/Arabidopsis_thaliana_DNA_v0")
        assert full.files, "resolve should attach siblings as files"
        head = await client.head(full.files[0].url)
        assert head.status_code < 400  # resolve URL serves (2xx/3xx)
