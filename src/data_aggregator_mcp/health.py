"""Best-effort upstream health probe, folded into list_sources(check_health=true).

Each probe is a direct timed GET (NOT the _http retry path) so a down endpoint
reports fast, not after backoff retries. A probe NEVER raises — health is
observability, not a hard dependency. Probes do not acquire a rate-limit token
(infrequent, opt-in, one-shot).

A probe asks the endpoint its adapter's search calls and reports "up" only on an
answer that adapter would accept: a 2xx whose body passes the adapter's own check.
A maintenance page, an in-band error envelope or a 3xx is "down", as it is to the
adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import httpx

from data_aggregator_mcp import _eutils, _http, datacite, huggingface, zenodo


class _Target(NamedTuple):
    url: str
    expect: type
    check: Callable[[Any], None]


_ESEARCH = f"{_eutils.BASE_URL}/esearch.fcgi"

_PROBE_TARGETS: dict[str, _Target] = {
    "zenodo": _Target(f"{zenodo.BASE_URL}/api/records?size=1", dict, zenodo._check_hits),
    "datacite": _Target(f"{datacite.BASE_URL}/dois?page[size]=1", dict, datacite._check_records),
    # literature searches PubMed (and OpenAIRE); omics searches SRA, GEO and BioProject.
    "omics": _Target(
        f"{_ESEARCH}?db=sra&term=human&retmax=0&retmode=json", dict, _eutils._check_esearch
    ),
    "literature": _Target(
        f"{_ESEARCH}?db=pubmed&term=test&retmax=0&retmode=json", dict, _eutils._check_esearch
    ),
    "huggingface": _Target(f"{huggingface.API}?limit=1", list, huggingface._check_datasets),
}
_TIMEOUT = 5.0
_DETAIL_MAX = 200


async def _probe_one(client: httpx.AsyncClient, name: str, target: _Target) -> dict:
    start = time.monotonic()
    try:
        resp = await client.request("GET", target.url, timeout=_TIMEOUT)
    except Exception as exc:  # transport, timeout or anything else: down, never raised
        return {"name": name, "status": "down", "latency_ms": None, "detail": _detail(repr(exc))}
    latency_ms = int((time.monotonic() - start) * 1000)
    if not resp.is_success:  # _http's success range: a 3xx is no answer to the adapter either
        return {
            "name": name,
            "status": "down",
            "latency_ms": latency_ms,
            "detail": f"HTTP {resp.status_code}",
        }
    try:
        _http._json_parser(target.expect, target.check)(resp)
    except Exception as exc:  # a 2xx the adapter would refuse: down, never raised
        return {
            "name": name,
            "status": "down",
            "latency_ms": latency_ms,
            "detail": _detail(f"HTTP {resp.status_code} with an unusable body: {exc!r}"),
        }
    return {"name": name, "status": "up", "latency_ms": latency_ms, "detail": None}


def _detail(text: str) -> str:
    return text[:_DETAIL_MAX]


async def probe_sources(client: httpx.AsyncClient) -> list[dict]:
    names = list(_PROBE_TARGETS)
    results = await asyncio.gather(*(_probe_one(client, n, _PROBE_TARGETS[n]) for n in names))
    return list(results)
