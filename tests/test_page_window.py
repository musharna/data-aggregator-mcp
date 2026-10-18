"""A page past an upstream's result window is refused up front, not read as an outage.

Zenodo and OpenAIRE serve only the first 10,000 records of a search: a page whose
``page * size`` passes 10,000 is answered with HTTP 400 ("Invalid querystring
parameters."; "Page * pageSize must be less than or equal to 10000"), which reached the
router as ``UpstreamUnavailableError``, an outage, on every page past the window (probed
2026-10-17: Zenodo sizes 10, 20, 25 served up to 10,000 exactly; OpenAIRE page 200 of 50
served, 201 refused). PubMed's 9,999 already had this guard (test_pubmed_answers.py).
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import openaire, zenodo
from data_aggregator_mcp.errors import ValidationError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_ZENODO_BODY = {"hits": {"hits": [], "total": 70164}}
_OPENAIRE_BODY = {"header": {"numFound": 4979103}, "results": []}


def _client(sent: list[httpx.Request], body: dict) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


@pytest.mark.parametrize(
    ("size", "offset", "last_served"),
    [(25, 10000, 9975), (10, 10000, 9990), (24, 9984, 9960)],  # 24: page 417 ends at 10,008
)
async def test_zenodo_refuses_a_page_past_its_window_and_serves_the_last_one(
    size, offset, last_served
) -> None:
    sent: list[httpx.Request] = []
    async with _client(sent, _ZENODO_BODY) as client:
        with pytest.raises(
            ValidationError,
            match=(
                r"^\[ValidationError\] Zenodo serves only the first 10000 records of a search; "
                rf"the page holding offset {offset} ends past them, so narrow the query$"
            ),
        ):
            await zenodo.search(client, "protein", size=size, offset=offset)
        assert sent == []
        # Positive control: the last page Zenodo serves is asked for and read.
        total, _ = await zenodo.search(client, "protein", size=size, offset=last_served)
    assert total == 70164
    assert int(sent[0].url.params["page"]) * size <= 10000


@pytest.mark.parametrize(("size", "offset", "last_served"), [(50, 10000, 9950), (30, 9990, 9960)])
async def test_openaire_refuses_a_page_past_its_window_and_serves_the_last_one(
    size, offset, last_served
) -> None:
    sent: list[httpx.Request] = []
    async with _client(sent, _OPENAIRE_BODY) as client:
        with pytest.raises(
            ValidationError,
            match=(
                r"^\[ValidationError\] OpenAIRE serves only the first 10000 records of a search; "
                rf"the page holding offset {offset} ends past them, so narrow the query$"
            ),
        ):
            await openaire.search(client, "protein", size=size, offset=offset)
        assert sent == []
        total, _ = await openaire.search(client, "protein", size=size, offset=last_served)
    assert total == 4979103
    assert int(sent[0].url.params["page"]) * size <= 10000


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_the_last_page_in_the_window_is_served_and_the_next_is_refused() -> None:
    async with httpx.AsyncClient() as client:
        z_total, z = await zenodo.search(client, "protein", size=25, offset=9975)
        o_total, o = await openaire.search(client, "protein", size=50, offset=9950)
        with pytest.raises(ValidationError, match="Zenodo serves only the first 10000"):
            await zenodo.search(client, "protein", size=25, offset=10000)
        with pytest.raises(ValidationError, match="OpenAIRE serves only the first 10000"):
            await openaire.search(client, "protein", size=50, offset=10000)
    assert z_total > 10000 and len(z) == 25
    assert o_total > 10000 and len(o) == 50
