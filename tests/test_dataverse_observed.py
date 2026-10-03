"""What the Dataverse lister sends and logs, pinned exactly (#88 mutant burn-down).

The answers it reads are in ``test_dataverse_answers.py``; these pin the installation
choice and the messages an operator sees when no listing is possible.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from data_aggregator_mcp import dataverse

_LOGGER = "data_aggregator_mcp.dataverse"


@pytest.fixture(autouse=True)
def no_env_base(monkeypatch):
    monkeypatch.delenv("DATAVERSE_BASE_URL", raising=False)


@pytest.mark.parametrize(
    ("landing", "base"),
    [
        ("https://dataverse.no/citation?persistentId=doi:10.18710/X", "https://dataverse.no"),
        ("http://dataverse.example.org/dataset.xhtml", "http://dataverse.example.org"),
        ("HTTPS://Dataverse.NL/x", "https://Dataverse.NL"),  # urlsplit lower-cases the scheme
        # Not a web URL, or no host: fall through to the DOI's own default (Harvard).
        ("ftp://dataverse.no/x", dataverse.DEFAULT_BASE_URL),
        ("https:///citation?persistentId=x", dataverse.DEFAULT_BASE_URL),
        ("dataverse.no/citation", dataverse.DEFAULT_BASE_URL),
        ("", dataverse.DEFAULT_BASE_URL),
        (None, dataverse.DEFAULT_BASE_URL),
    ],
)
def test_the_installation_is_the_landing_urls_web_host(landing, base):
    assert dataverse._base_url("10.7910/DVN/X", landing) == base


def test_a_landing_url_without_a_host_is_no_installation_for_another_prefix():
    assert dataverse._base_url("10.18710/X", "ftp://dataverse.no/x") is None
    assert dataverse._base_url("10.18710/X", "https://dataverse.no/x") == "https://dataverse.no"


@pytest.mark.parametrize(
    ("env", "base"),
    [
        ("https://darus.uni-stuttgart.de/", "https://darus.uni-stuttgart.de"),
        ("https://data.example.org/dvX//", "https://data.example.org/dvX"),
        ("/https://data.example.org", "/https://data.example.org"),  # only the right end
    ],
)
def test_the_operators_base_url_loses_only_its_trailing_slashes(monkeypatch, env, base):
    monkeypatch.setenv("DATAVERSE_BASE_URL", env)
    assert dataverse._base_url("10.18419/X", None) == base
    # The record's own landing URL still wins over the operator's setting.
    assert dataverse._base_url("10.18419/X", "https://dataverse.no/x") == "https://dataverse.no"


def test_only_harvards_prefix_defaults_to_harvard():
    assert dataverse._base_url("10.7910/DVN/TJCLKP", None) == "https://dataverse.harvard.edu"
    assert dataverse._base_url("10.79101/X", None) is None
    assert dataverse._base_url("10.18710/F79PSN", None) is None


def test_a_malformed_landing_url_is_logged_with_what_was_wrong(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert dataverse._base_url("10.7910/DVN/X", "https://[::1/x") == dataverse.DEFAULT_BASE_URL
    assert [r.getMessage() for r in caplog.records] == [
        "ignoring malformed landing URL 'https://[::1/x' for 10.7910/DVN/X: Invalid IPv6 URL"
    ]


async def test_an_unknown_installation_is_logged_and_nothing_is_sent(caplog):
    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        async with httpx.AsyncClient(transport=transport) as c:
            assert await dataverse.files(c, "10.18710/F79PSN") == []
    assert sent == []
    assert [r.getMessage() for r in caplog.records] == [
        "Dataverse DOI 10.18710/F79PSN: installation unknown (no landing URL, no "
        "DATAVERSE_BASE_URL, not a Harvard 10.7910 DOI); no file listing attempted"
    ]


@pytest.mark.parametrize(
    ("routes", "service"),
    [
        ({"/api/datasets/:persistentId/": 503}, "Dataverse dataset"),
        (
            {"/api/datasets/:persistentId/": 404, "/api/files/:persistentId/": 503},
            "Dataverse file",
        ),
    ],
)
async def test_an_outage_names_the_endpoint_that_failed(monkeypatch, routes, service):
    from data_aggregator_mcp import _http
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(routes[request.url.path], text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=rf"^\[UpstreamUnavailableError\] {service} exhausted 3 retries \(last HTTP 503\)$",
        ):
            await dataverse.files(c, "10.7910/DVN/X")
    assert {(r.method, r.url.params["persistentId"]) for r in sent} == {
        ("GET", "doi:10.7910/DVN/X")
    }
