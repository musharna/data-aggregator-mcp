#!/usr/bin/env python3
"""Recall-lift eval harness for ``search(understand=true)`` (A2.P1).

Runs each natural-language query in ``eval_understand_fixture.json`` twice — with
``understand=false`` (raw keyword query) and ``understand=true`` (LLM NL→structured
rewrite) — computes recall@20 against the fixture's known-relevant ids, and PRINTS the
per-query + mean lift. This is a "show it works" instrument, NOT a pytest assertion:
live recall varies run to run, the labeled set is small/illustrative, and the LLM is
nondeterministic in general (we pin temperature=0 but endpoints differ).

Gated: requires BOTH ``DATA_AGGREGATOR_MCP_LIVE=1`` (real upstream APIs) and an LLM
endpoint (``LLM_API_BASE``). Exits early with a message if either is missing.

Usage:
    DATA_AGGREGATOR_MCP_LIVE=1 LLM_API_BASE=https://... [LLM_API_KEY=...] \
        python scripts/eval_understand.py

Interpreting the output: a POSITIVE mean lift means understanding surfaced more
known-relevant ids in the top-20 across the set; per-query lifts of 0 are expected for
queries with no extractable structure. A negative lift on a single query is possible
(the rewrite narrowed too far) — what matters is the aggregate signal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from data_aggregator_mcp import router

_FIXTURE = Path(__file__).with_name("eval_understand_fixture.json")
_K = 20


def _result_ids(result) -> set[str]:
    """All identifiers a result row might match a fixture id by (id + doi)."""
    ids: set[str] = set()
    for r in result.results:
        if r.id:
            ids.add(r.id.lower())
        if r.doi:
            ids.add(r.doi.lower())
    return ids


def _recall_at_k(found: set[str], relevant: list[str]) -> float:
    if not relevant:
        return 0.0
    rel = {x.lower() for x in relevant}
    hit = sum(1 for x in rel if x in found)
    return hit / len(rel)


async def _run_one(client: httpx.AsyncClient, query: str, *, understand: bool) -> set[str]:
    result = await router.search_page(client, query=query, size=_K, understand=understand)
    return _result_ids(result)


async def main() -> int:
    if os.environ.get("DATA_AGGREGATOR_MCP_LIVE") != "1":
        print("SKIP: set DATA_AGGREGATOR_MCP_LIVE=1 to run the live eval.")
        return 0
    if not os.environ.get("LLM_API_BASE"):
        print("SKIP: set LLM_API_BASE (an OpenAI-compatible /chat/completions endpoint) to run.")
        return 0

    fixture = json.loads(_FIXTURE.read_text())
    queries = fixture["queries"]
    lifts: list[float] = []

    print(f"Recall@{_K} — understand off vs on  ({len(queries)} queries)\n")
    print(f"{'off':>6}  {'on':>6}  {'lift':>6}  query")
    print("-" * 72)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for q in queries:
            query, relevant = q["query"], q["relevant"]
            off_ids = await _run_one(client, query, understand=False)
            on_ids = await _run_one(client, query, understand=True)
            off_r = _recall_at_k(off_ids, relevant)
            on_r = _recall_at_k(on_ids, relevant)
            lift = on_r - off_r
            lifts.append(lift)
            print(f"{off_r:6.2f}  {on_r:6.2f}  {lift:+6.2f}  {query[:48]}")

    mean_lift = sum(lifts) / len(lifts) if lifts else 0.0
    print("-" * 72)
    print(f"mean recall@{_K} lift (on - off): {mean_lift:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
