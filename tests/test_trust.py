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


@pytest.mark.asyncio
async def test_annotate_crossref_5xx_is_unknown_not_raised():
    # spec §8: enrichment never raises into a valid resolve. A Crossref outage
    # (exhausted retries) must degrade to unknown (all-None), not abort resolve.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, json={}))
    ) as c:
        t = await trust.annotate(c, _resource(doi="10.1/x"))
    assert t.retracted is None and t.retraction_doi is None and t.concern is None


_ENTRY_DOI = "10.2210/pdb7mwh/pdb"  # live pdb:7MWH: the structure's own (clean) DOI
_PAPER_DOI = "10.1016/j.heliyon.2022.e09873"  # its primary citation, retracted 2022


def _structure(paper_doi: str = _PAPER_DOI):
    """PDB-shaped record (post B-H4): own entry DOI + the paper as a described_in link,
    plus a non-DOI described_in target (router's plant-genomics bridge) to be skipped."""
    from data_aggregator_mcp.models import Link

    return DataResource(
        id="pdb:7MWH",
        source="pdb",
        kind="dataset",
        title="Crystal structure of BAZ2A with DNA",
        doi=_ENTRY_DOI,
        links=[
            Link(rel="landing_page", target_id="https://www.rcsb.org/structure/7MWH"),
            Link(rel="described_in", target_id=paper_doi),
            Link(rel="described_in", target_id="plant-genomics:taxid:4081"),
        ],
    )


def _crossref_by_doi(bodies):
    """Handler answering per requested DOI; any DOI not in ``bodies`` is a test bug."""
    from urllib.parse import unquote

    def handler(request):
        doi = unquote(request.url.path.removeprefix("/works/"))
        if doi not in bodies:
            raise AssertionError(f"unexpected Crossref lookup {doi!r}")
        status, body = bodies[doi]
        return httpx.Response(status, json=body)

    return handler


@pytest.mark.asyncio
async def test_annotate_checks_described_in_paper_dois():
    """A record's described_in paper is part of its integrity: a PDB entry whose own DOI
    is clean but whose primary-citation paper is retracted must report retracted."""
    notice = {"type": "retraction", "DOI": "10.1016/j.heliyon.2022.e09873"}
    handler = _crossref_by_doi({_ENTRY_DOI: (200, _msg([])), _PAPER_DOI: (200, _msg([notice]))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        t = await trust.annotate(c, _structure())
    assert t.retracted is True and t.retraction_doi == "10.1016/j.heliyon.2022.e09873"
    # positive control: same shape, clean paper -> a definitive not-retracted
    clean = _crossref_by_doi({_ENTRY_DOI: (200, _msg([])), "10.1/clean": (200, _msg([]))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(clean)) as c:
        t = await trust.annotate(c, _structure(paper_doi="10.1/clean"))
    assert t.retracted is False and t.concern is False


@pytest.mark.asyncio
async def test_annotate_paper_check_outage_is_unknown_not_clean():
    """If the paper lookup fails, a clean own-DOI must not become a false 'not retracted'."""
    handler = _crossref_by_doi({_ENTRY_DOI: (200, _msg([])), _PAPER_DOI: (503, {})})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        t = await trust.annotate(c, _structure())
    assert t.retracted is None and t.concern is None


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


@_live_only
@pytest.mark.asyncio
async def test_live_pdb_entry_with_retracted_paper():
    """pdb:7MWH's own DOI is clean; its paper (Heliyon 2022) is retracted. Control:
    pdb:6VXX (Walls et al., Cell 2020) is not."""
    from data_aggregator_mcp import pdb

    async with httpx.AsyncClient(timeout=60) as c:
        retr = await trust.annotate(c, await pdb.resolve(c, "pdb:7MWH"))
        clean = await trust.annotate(c, await pdb.resolve(c, "pdb:6VXX"))
    assert retr.retracted is True and retr.retraction_doi
    assert clean.retracted is False
