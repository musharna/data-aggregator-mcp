"""Every request that puts a DOI into a URL path percent-encodes it (L25's class).

DOIs may legally contain ``#``, ``?``, ``;`` and ``<>`` (SICI DOIs do). Sent raw, a
``#`` ends the path as a fragment and the upstream is asked about a different DOI.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from data_aggregator_mcp import _ratelimit, citation, datacite, fulltext, trust
from data_aggregator_mcp.models import DataResource

SICI = "10.1002/(SICI)1097-4636(199706)35:4<477::AID-JBM8>3.0.CO;2-#"
PLAIN = "10.5061/dryad.abc"


@pytest.fixture(autouse=True)
def _no_ratelimit(monkeypatch) -> None:
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(_ratelimit, "acquire", _noop)


async def _requested_path(call, doi: str) -> str:
    seen: list[httpx.URL] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Only the URL that went out is under test; the 404 outcome is irrelevant.
        with contextlib.suppress(Exception):
            await call(client, doi)
    assert seen, "no request was made"
    assert not seen[0].fragment, f"DOI tail was sent as a fragment: {seen[0]}"
    return seen[0].raw_path.decode().split("?", 1)[0]


def _res(doi: str) -> DataResource:
    return DataResource(id="x:1", source="x", kind="dataset", title="t", doi=doi)


async def _unpaywall(client, doi):
    return await fulltext._unpaywall(client, doi)


async def _datacite(client, doi):
    return await datacite.resolve(client, f"datacite:{doi}")


async def _crossref(client, doi):
    return await trust.annotate(client, _res(doi))


async def _citation(client, doi):
    return await citation.render(client, _res(doi), "bibtex")


@pytest.mark.parametrize(
    "call", [_unpaywall, _datacite, _crossref, _citation], ids=lambda f: f.__name__
)
async def test_doi_is_percent_encoded_in_the_request_path(monkeypatch, call) -> None:
    monkeypatch.setenv("UNPAYWALL_EMAIL", "ci@example.org")
    path = await _requested_path(call, SICI)
    assert "%23" in path and "%3C477" in path, path  # '#' and '<' survived, encoded
    # Positive control: an ordinary DOI goes out unchanged, '/' not encoded.
    assert (await _requested_path(call, PLAIN)).endswith("/10.5061/dryad.abc")
