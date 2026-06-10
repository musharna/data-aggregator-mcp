import os

import httpx
import pytest

from data_aggregator_mcp import trust
from data_aggregator_mcp.models import DataResource, TrustSignals


def test_trust_signals_defaults_all_none():
    t = TrustSignals()
    assert t.retracted is None and t.retraction_doi is None and t.concern is None


def test_dataresource_has_optional_trust_field():
    r = DataResource(id="x:1", source="x", kind="publication", title="t")
    assert r.trust is None


def _resource(doi=None, ident=None):
    r = DataResource(id="pub:1", source="literature", kind="publication", title="t", doi=doi)
    if ident:
        r.identifiers["doi"] = ident
    return r


def _msg(updated_by):
    return {"message": {"updated-by": updated_by}}


@pytest.mark.asyncio
async def test_annotate_flags_retracted_doi():
    notice = {"type": "retraction", "label": "Retraction", "DOI": "10.1/notice"}
    body = _msg([{"type": "correction", "DOI": "10.1/c"}, notice])

    async def handler(request):
        assert request.url.host == "api.crossref.org"
        assert "10.1016" in str(request.url)  # the resource DOI, url-encoded
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        t = await trust.annotate(c, _resource(doi="10.1016/S0140-6736(97)11096-0"))
    assert t.retracted is True and t.retraction_doi == "10.1/notice" and t.concern is False


@pytest.mark.asyncio
async def test_annotate_clean_work_is_false_not_none():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_msg([])))
    ) as c:
        t = await trust.annotate(c, _resource(doi="10.1038/nature14539"))
    assert t.retracted is False and t.retraction_doi is None and t.concern is False


@pytest.mark.asyncio
async def test_annotate_flags_expression_of_concern():
    body = _msg([{"type": "expression_of_concern", "DOI": "10.1/eoc"}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        t = await trust.annotate(c, _resource(doi="10.1/x"))
    assert t.retracted is False and t.concern is True


@pytest.mark.asyncio
async def test_annotate_404_is_unknown_all_none():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as c:
        t = await trust.annotate(c, _resource(doi="10.5061/dryad.notcrossref"))
    assert t.retracted is None and t.retraction_doi is None and t.concern is None


@pytest.mark.asyncio
async def test_annotate_no_doi_makes_no_call():
    def boom(request):
        raise AssertionError("must not hit the network when the resource has no DOI")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        t = await trust.annotate(c, _resource(doi=None))
    assert t.retracted is None


@pytest.mark.asyncio
async def test_annotate_uses_identifiers_doi_fallback():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_msg([])))
    ) as c:
        t = await trust.annotate(c, _resource(doi=None, ident="10.1038/nature14539"))
    assert t.retracted is False  # used identifiers["doi"]


@pytest.mark.asyncio
async def test_annotate_non_dict_body_is_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))
    ) as c:
        t = await trust.annotate(c, _resource(doi="10.1/x"))
    assert t.retracted is None


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_retracted_and_clean():
    async with httpx.AsyncClient(timeout=60) as c:
        retr = await trust.annotate(c, _resource(doi="10.1016/S0140-6736(97)11096-0"))
        assert retr.retracted is True and retr.retraction_doi
        clean = await trust.annotate(c, _resource(doi="10.1038/nature14539"))
        assert clean.retracted is False
        unknown = await trust.annotate(c, _resource(doi="10.5061/dryad.0000000zz"))
        assert unknown.retracted is None  # not a Crossref work
