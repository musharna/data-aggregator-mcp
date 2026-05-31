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
