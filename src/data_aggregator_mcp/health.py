"""Best-effort upstream health probe, folded into list_sources(check_health=true).

Each probe is a direct timed GET (NOT the _http retry path) so a down endpoint
reports fast, not after backoff retries. A probe NEVER raises — health is
observability, not a hard dependency. Probes do not acquire a rate-limit token
(infrequent, opt-in, one-shot)."""

from __future__ import annotations

import asyncio
import time

import httpx

_PROBE_TARGETS: dict[str, str] = {
    "zenodo": "https://zenodo.org/api/records?size=1",
    "datacite": "https://api.datacite.org/heartbeat",
    "omics": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
    "literature": (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=test&format=json&pageSize=1"
    ),
    "huggingface": "https://huggingface.co/api/datasets?limit=1",
}
_TIMEOUT = 5.0


async def _probe_one(client: httpx.AsyncClient, name: str, url: str) -> dict:
    start = time.monotonic()
    try:
        resp = await client.request("GET", url, timeout=_TIMEOUT)
    except Exception as exc:  # transport / timeout — report down, never raise
        return {"name": name, "status": "down", "latency_ms": None, "detail": repr(exc)[:200]}
    latency_ms = int((time.monotonic() - start) * 1000)
    if 200 <= resp.status_code < 400:
        return {"name": name, "status": "up", "latency_ms": latency_ms, "detail": None}
    return {
        "name": name,
        "status": "down",
        "latency_ms": latency_ms,
        "detail": f"HTTP {resp.status_code}",
    }


async def probe_sources(client: httpx.AsyncClient) -> list[dict]:
    names = list(_PROBE_TARGETS)
    results = await asyncio.gather(*(_probe_one(client, n, _PROBE_TARGETS[n]) for n in names))
    return list(results)
