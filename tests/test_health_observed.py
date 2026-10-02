"""What the health probe sends, and the exact report for each kind of answer.

A probe is "up" only on an answer its adapter would accept. Each case below is a way an
upstream can be down or broken while still answering something: a maintenance page, an
in-band error envelope, a 3xx, a 429, a 5xx, a transport error, or an exception nobody
expected. Each must be reported as down, with the status, latency and detail pinned, and
none may raise.
"""

from __future__ import annotations

import itertools
import os
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _eutils, datacite, health, huggingface, server, zenodo

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

# The smallest answer each adapter's own check accepts.
_ACCEPTED: dict[str, Any] = {
    "zenodo": {"hits": {"hits": [], "total": 0}},
    "datacite": {"data": [], "meta": {"total": 0}},
    "omics": {"esearchresult": {"count": "7", "idlist": []}},
    "literature": {"esearchresult": {"count": "7", "idlist": []}},
    "huggingface": [],
}
_MAINTENANCE = "<html><body><h1>Down for maintenance</h1></body></html>"


class _Client:
    """Records each request; answers with ``answer(url)`` (a Response, or raises it)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        out = self.answer(url)
        if isinstance(out, BaseException):
            raise out
        return out


def _accepting_client() -> _Client:
    by_url = {t.url: _ACCEPTED[name] for name, t in health._PROBE_TARGETS.items()}
    return _Client(lambda url: httpx.Response(200, json=by_url[url]))


@pytest.fixture
def clock(monkeypatch):
    """time.monotonic for the probe only: 1.0 at each start, 3.0 at each answer (2000 ms).

    Patched on the module's own ``time`` name, so the event loop's clock is untouched."""
    ticks = itertools.cycle([1.0, 3.0])
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: next(ticks)))


def _target(check=lambda body: None, expect: type = dict) -> health._Target:
    return health._Target("https://probe.test/x?y=1", expect, check)


async def _probe(answer: Any, target: health._Target | None = None) -> dict:
    return await health._probe_one(_Client(lambda url: answer), "zenodo", target or _target())


# --- the answers a down endpoint gives, reported as down (seen to fail on the old code) ---


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (
            httpx.Response(200, text=_MAINTENANCE, headers={"content-type": "text/html"}),
            "HTTP 200 with an unusable body: "
            "JSONDecodeError('Expecting value: line 1 column 1 (char 0)')",
        ),
        (
            httpx.Response(302, headers={"location": "https://login.test/"}),
            "HTTP 302",
        ),
        (httpx.Response(304), "HTTP 304"),
    ],
    ids=["maintenance-page", "redirect-to-login", "not-modified"],
)
async def test_an_answer_the_adapter_refuses_is_down(answer, detail) -> None:
    down = await health.probe_sources(_Client(lambda url: answer))
    assert [(r["name"], r["status"], r["detail"]) for r in down] == [
        (name, "down", detail) for name in health._PROBE_TARGETS
    ]
    # Positive control: the answers each adapter accepts are up.
    up = await health.probe_sources(_accepting_client())
    assert [(r["name"], r["status"], r["detail"]) for r in up] == [
        (name, "up", None) for name in health._PROBE_TARGETS
    ]


async def test_an_ncbi_error_inside_a_200_is_down() -> None:
    """NCBI reports a failed search inside a 200 (LESSONS 2026-09-22)."""
    envelope = {"esearchresult": {"ERROR": "Search Backend failed"}}
    client = _Client(lambda url: httpx.Response(200, json=envelope))
    by_name = {r["name"]: r for r in await health.probe_sources(client)}
    for name in ("omics", "literature"):
        assert by_name[name]["status"] == "down"
        assert by_name[name]["detail"] == (
            "HTTP 200 with an unusable body: "
            "UpstreamEnvelopeError('NCBI esearch ERROR: Search Backend failed')"
        )
    up = {r["name"]: r["status"] for r in await health.probe_sources(_accepting_client())}
    assert up["omics"] == up["literature"] == "up"


async def test_each_probe_asks_the_endpoint_its_adapter_searches() -> None:
    """Not Europe PMC for literature (its search is PubMed + OpenAIRE), not DataCite's
    heartbeat or NCBI's einfo (no adapter reads either)."""
    client = _Client(lambda url: httpx.Response(503))
    results = await health.probe_sources(client)
    assert [r["name"] for r in results] == list(health._PROBE_TARGETS)
    assert [url for _, url, _ in client.calls] == [
        "https://zenodo.org/api/records?size=1",
        "https://api.datacite.org/dois?page[size]=1",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        "?db=sra&term=human&retmax=0&retmode=json",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        "?db=pubmed&term=test&retmax=0&retmode=json",
        "https://huggingface.co/api/datasets?limit=1",
    ]


def test_each_probe_is_judged_by_its_adapters_own_check() -> None:
    assert {n: (t.expect, t.check) for n, t in health._PROBE_TARGETS.items()} == {
        "zenodo": (dict, zenodo._check_hits),
        "datacite": (dict, datacite._check_records),
        "omics": (dict, _eutils._check_esearch),
        "literature": (dict, _eutils._check_esearch),
        "huggingface": (list, huggingface._check_datasets),
    }


# --- the exact report and request (pin the existing behaviour) ---


async def test_the_request_is_one_get_with_the_probe_timeout() -> None:
    client = _accepting_client()
    await health.probe_sources(client)
    assert client.calls == [
        ("GET", t.url, {"timeout": 5.0}) for t in health._PROBE_TARGETS.values()
    ]


@pytest.mark.parametrize("status", [200, 201, 299])
async def test_a_2xx_with_an_accepted_body_is_up(clock, status) -> None:
    r = await _probe(httpx.Response(status, json={}))
    assert r == {"name": "zenodo", "status": "up", "latency_ms": 2000, "detail": None}


@pytest.mark.parametrize("status", [199, 300, 301, 404, 429, 500, 503])
async def test_a_non_2xx_is_down_with_its_status_and_latency(clock, status) -> None:
    r = await _probe(httpx.Response(status, json={}))
    assert r == {"name": "zenodo", "status": "down", "latency_ms": 2000, "detail": f"HTTP {status}"}


async def test_a_body_the_check_refuses_is_down_with_the_reason(clock) -> None:
    def check(body: dict) -> None:
        if "hits" not in body:
            raise ValueError("no hits")

    r = await _probe(httpx.Response(200, json={"error": "x"}), _target(check))
    assert r == {
        "name": "zenodo",
        "status": "down",
        "latency_ms": 2000,
        "detail": "HTTP 200 with an unusable body: ValueError('no hits')",
    }
    ok = await health._probe_one(
        _Client(lambda url: httpx.Response(200, json={"hits": 1})), "zenodo", _target(check)
    )
    assert ok["status"] == "up"


async def test_a_wrong_top_level_type_is_down() -> None:
    r = await _probe(httpx.Response(200, json={"error": "rate"}), _target(expect=list))
    assert r["status"] == "down"
    assert r["detail"] == (
        "HTTP 200 with an unusable body: "
        """UnexpectedShapeError('expected list JSON, got dict: {"error":"rate"}')"""
    )
    assert (await _probe(httpx.Response(200, json=[]), _target(expect=list)))["status"] == "up"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.TooManyRedirects("loop"),
        RuntimeError("nobody expected this"),
    ],
)
async def test_any_exception_from_the_request_is_down_never_raised(exc) -> None:
    r = await _probe(exc)
    assert r == {"name": "zenodo", "status": "down", "latency_ms": None, "detail": repr(exc)}
    assert (await _probe(httpx.Response(200, json={})))["status"] == "up"


async def test_an_unexpected_exception_inside_the_check_is_down_never_raised(clock) -> None:
    def check(body: dict) -> None:
        raise KeyError("hits")

    r = await _probe(httpx.Response(200, json={}), _target(check))
    assert r == {
        "name": "zenodo",
        "status": "down",
        "latency_ms": 2000,
        "detail": "HTTP 200 with an unusable body: KeyError('hits')",
    }


async def test_a_detail_is_cut_at_200_characters() -> None:
    exc = httpx.ConnectError("x" * 500)
    r = await _probe(exc)
    assert r["detail"] == repr(exc)[:200]
    assert len(r["detail"]) == 200

    def check(body: dict) -> None:
        raise ValueError("y" * 500)

    r = await _probe(httpx.Response(200, json={}), _target(check))
    assert len(r["detail"]) == 200
    assert r["detail"].startswith("HTTP 200 with an unusable body: ValueError('yyy")


async def test_probe_sources_reports_each_source_in_order_with_its_own_answer() -> None:
    zen = health._PROBE_TARGETS["zenodo"].url
    accepting = _accepting_client()

    def answer(url: str) -> Any:
        return httpx.Response(503) if url == zen else accepting.answer(url)

    results = await health.probe_sources(_Client(answer))
    assert [(r["name"], r["status"]) for r in results] == [
        ("zenodo", "down"),
        ("datacite", "up"),
        ("omics", "up"),
        ("literature", "up"),
        ("huggingface", "up"),
    ]


# --- live ---


@live_only
async def test_live_every_probe_is_up_through_the_server_client() -> None:
    """The real client (redirects followed, egress hook on): each probe URL answers
    directly, without a redirect, with a body its adapter accepts."""
    answered: list[tuple[int, str]] = []

    async def record(resp: httpx.Response) -> None:
        answered.append((resp.status_code, str(resp.request.url)))

    async with server._http_client() as client:
        client.event_hooks["response"].append(record)
        results = await health.probe_sources(client)
    assert [(r["name"], r["status"], r["detail"]) for r in results] == [
        (name, "up", None) for name in health._PROBE_TARGETS
    ]
    assert all(isinstance(r["latency_ms"], int) for r in results)
    assert sorted(answered) == sorted(
        (200, str(httpx.URL(t.url))) for t in health._PROBE_TARGETS.values()
    )
