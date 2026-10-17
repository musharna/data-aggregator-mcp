"""Perf ratchet: exact counts of work per workload, never milliseconds.

    uv run python benchmarks/perf/ratchet.py                   # gate: exit 1 on any change
    uv run python benchmarks/perf/ratchet.py --update          # lock in a drop
    uv run python benchmarks/perf/ratchet.py --update --allow-increase   # accept a rise

Each workload drives one tool through ``server._dispatch`` against the fake upstreams
in ``upstream.py``, in its own interpreter (``PYTHONHASHSEED=0``, fresh caches). Two
counts are taken without editing the code under test:

- **calls**: Python function calls under ``src/data_aggregator_mcp``, from
  ``sys.setprofile``, keyed ``relpath:qualname``. A coroutine or generator counts once
  when it starts, not on every resume, so the event loop's scheduling cannot move the
  number. Comprehension frames are skipped: 3.11 runs them as functions, 3.12+ inlines
  them (PEP 709), and the count must be the same on every CI Python.
- **http**: requests per upstream host.

The gate compares each workload's totals with ``baseline.json`` and must be EQUAL: a
rise fails unless ``--allow-increase`` writes it, and a drop fails until ``--update``
writes it, so a gain lands in the PR that earned it. On failure it prints the
functions whose counts moved most. Each workload also reports an ``output`` digest of
what only that workload returns (ids, file hashes), which must match the baseline too:
a harness that ran the wrong workload, or none, cannot pass on its echoed name.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import FrameType
from typing import Any

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"
QUERY = "coral heat stress"
# Comprehensions are frames on 3.11 only (PEP 709 inlines them on 3.12+).
_INLINED = frozenset({"<listcomp>", "<dictcomp>", "<setcomp>"})
_RESUMABLE = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


# --- workloads (run in the child) ---------------------------------------------------


async def _search_fanout(server: Any, tmp: Path) -> dict:
    out = await server._dispatch(
        "search",
        {"query": QUERY, "sources": ["zenodo", "datacite", "huggingface"], "size": 10},
    )
    ids = [r["id"] for r in out["results"]]
    return {"results": len(ids), "errors": sorted(out["errors"]), "ids": _digest(ids)}


async def _cursor_paging(server: Any, tmp: Path) -> dict:
    args: dict[str, Any] = {"query": QUERY, "sources": ["zenodo", "datacite"], "size": 5}
    ids: list[str] = []
    pages = 0
    while True:
        out = await server._dispatch("search", args)
        pages += 1
        ids += [r["id"] for r in out["results"]]
        if not out.get("next_cursor") or pages == 4:
            break
        args = {"cursor": out["next_cursor"]}
    return {"pages": pages, "results": len(ids), "distinct": len(set(ids)), "ids": _digest(ids)}


async def _resolve(server: Any, tmp: Path) -> dict:
    got = []
    # The second zenodo id is the first again: a resolve-cache hit.
    for rid in ("10.5281/zenodo.900001", "10.5061/dryad.perf001", "hf:reef1/coral-heat-0",
                "10.5281/zenodo.900001"):  # fmt: skip
        r = await server._dispatch("resolve", {"id": rid})
        got.append([r["id"], r["source"], [f["name"] for f in r["files"]]])
    return {"resolved": len(got), "records": _digest(got)}


async def _fetch(server: Any, tmp: Path) -> dict:
    from upstream import FETCH_RECORD  # noqa: PLC0415 - the child puts HERE on sys.path

    dest = tmp / "dl"
    out = await server._dispatch("fetch", {"id": f"zenodo:{FETCH_RECORD}", "dest": str(dest)})
    files = sorted(p for p in dest.rglob("*.csv"))
    hashes = [[p.name, hashlib.sha256(p.read_bytes()).hexdigest()] for p in files]
    return {"files": len(hashes), "reported": len(out["paths"]), "bytes": out["bytes"],
            "sha256": _digest(hashes)}  # fmt: skip


WORKLOADS: dict[str, Callable[[Any, Path], Awaitable[dict]]] = {
    "search_fanout": _search_fanout,
    "cursor_paging": _cursor_paging,
    "resolve": _resolve,
    "fetch": _fetch,
}


class CallCounter:
    def __init__(self, src_root: Path) -> None:
        self.root = str(src_root) + os.sep
        self.counts: Counter[str] = Counter()
        self._keys: dict[Any, str | None] = {}
        self._live: dict[int, FrameType] = {}  # started generator/coroutine frames

    def _key(self, code: Any) -> str | None:
        if code not in self._keys:
            fn = code.co_filename
            ok = fn.startswith(self.root) and code.co_name not in _INLINED
            self._keys[code] = f"{fn[len(self.root) :]}:{code.co_qualname}" if ok else None
        return self._keys[code]

    def __call__(self, frame: FrameType, event: str, arg: Any) -> None:
        if event != "call":
            return
        code = frame.f_code
        key = self._key(code)
        if key is None:
            return
        if code.co_flags & _RESUMABLE:
            # Holding the frame keeps its id from being reused by a later frame.
            if self._live.get(id(frame)) is frame:
                return  # a resume, not a call
            self._live[id(frame)] = frame
        self.counts[key] += 1


def child(name: str) -> dict:
    """Run one workload in this process and return its counts."""
    sys.path.insert(0, str(HERE))
    from upstream import Upstream  # noqa: PLC0415

    import data_aggregator_mcp
    from data_aggregator_mcp import _ratelimit, server

    if name not in WORKLOADS:
        raise SystemExit(f"unknown workload {name!r}; known: {sorted(WORKLOADS)}")
    # Pacing is the one clock the code branches on: a bucket that never runs dry makes
    # every acquire a single pass. The egress guard resolves DNS for every host; nothing
    # here leaves the process, so it is off, as in tests/conftest.py.
    _ratelimit._DEFAULT_RATE = 1e9
    os.environ["DATA_AGGREGATOR_MCP_ALLOW_PRIVATE_EGRESS"] = "1"
    upstream = Upstream()
    upstream.install()
    counter = CallCounter(Path(data_aggregator_mcp.__file__).resolve().parent)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ[_ratelimit.STATE_DIR_ENV] = tmp

        async def main() -> dict:
            async with server.shared_http_client():
                sys.setprofile(counter)
                try:
                    return await WORKLOADS[name](server, Path(tmp))
                finally:
                    sys.setprofile(None)

        output = asyncio.run(main())
    if upstream.unstubbed:
        raise SystemExit(f"{name}: requests the fake upstream cannot answer: {upstream.unstubbed}")
    return {
        "calls_total": sum(counter.counts.values()),
        "http_total": sum(upstream.requests.values()),
        "http": dict(sorted(upstream.requests.items())),
        "output": output,
        "calls": dict(sorted(counter.counts.items())),
    }


# --- gate (run in the parent) -------------------------------------------------------

# App configuration the server reads (tests/conftest.py _APP_ENV_VARS): none may leak in.
_SCRUB = ("NCBI_API_KEY", "NCBI_EMAIL", "UNPAYWALL_EMAIL", "CACHE_TTL_SECONDS", "LLM_API_KEY",
          "LLM_API_BASE", "LLM_MODEL", "EMBEDDING_API_KEY", "EMBEDDING_API_BASE",
          "EMBEDDING_MODEL", "DATA_GOV_API_KEY", "DATAVERSE_BASE_URL",
          "DATA_AGGREGATOR_MCP_STATE_DIR", "DATA_AGGREGATOR_MCP_ALLOW_FILE_URLS")  # fmt: skip


def measure(name: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONHASHSEED"] = "0"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", name],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"workload {name} failed (exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


def top_deltas(old: dict[str, int], new: dict[str, int], n: int = 10) -> list[str]:
    keys = set(old) | set(new)
    moved = [(new.get(k, 0) - old.get(k, 0), k) for k in keys if new.get(k, 0) != old.get(k, 0)]
    moved.sort(key=lambda d: (-abs(d[0]), d[1]))
    return [f"    {d:+d}  {k}  ({old.get(k, 0)} -> {new.get(k, 0)})" for d, k in moved[:n]]


def compare(name: str, base: dict | None, now: dict) -> tuple[str, list[str]]:
    """``("equal"|"lower"|"higher"|"new"|"output", report lines)`` for one workload."""
    if base is None:
        return "new", [f"{name}: not in the baseline"]
    lines = []
    if base["output"] != now["output"]:
        lines.append(f"{name}: OUTPUT changed {base['output']} -> {now['output']}")
        return "output", lines
    tot = ("calls_total", "http_total")
    diffs = {k: now[k] - base[k] for k in tot if now[k] != base[k]}
    hosts = set(base["http"]) | set(now["http"])
    host_d = {h: now["http"].get(h, 0) - base["http"].get(h, 0) for h in sorted(hosts)}
    host_d = {h: d for h, d in host_d.items() if d}
    if not diffs and not host_d:
        return "equal", [f"{name}: equal ({now['calls_total']} calls, {now['http_total']} http)"]
    up = any(d > 0 for d in [*diffs.values(), *host_d.values()])
    for k, d in diffs.items():
        lines.append(f"{name}: {k} {base[k]} -> {now[k]} ({d:+d})")
    for h, d in host_d.items():
        lines.append(
            f"{name}: http {h} {base['http'].get(h, 0)} -> {now['http'].get(h, 0)} ({d:+d})"
        )
    lines += top_deltas(base["calls"], now["calls"])
    return ("higher" if up else "lower"), lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--update", action="store_true", help="write lowered counts")
    ap.add_argument("--allow-increase", action="store_true", help="with --update: accept rises")
    ap.add_argument(
        "--accept-output",
        action="store_true",
        help="with --update: accept a changed output digest (the workload now does something else)",
    )
    ap.add_argument("--only", default="", help="comma-separated workloads")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    args = ap.parse_args(argv)
    if args.child:
        print(json.dumps(child(args.child)))
        return 0
    names = args.only.split(",") if args.only else list(WORKLOADS)
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
    failed = False
    for name in names:
        now = measure(name)
        verdict, lines = compare(name, baseline.get(name), now)
        print("\n".join(lines))
        if verdict == "equal":
            continue
        # One flag per kind of change: a count rise never carries a behaviour change in.
        ok = args.update and (
            verdict == "lower"
            or (verdict in ("higher", "new") and args.allow_increase)
            or (verdict == "output" and args.accept_output)
        )
        if ok:
            baseline[name] = now
            print(f"{name}: baseline updated ({verdict})")
        else:
            failed = True
            hint = {
                "lower": "--update",
                "higher": "--update --allow-increase",
                "new": "--update --allow-increase",
            }.get(verdict, "--update --accept-output (the workload's output changed)")
            print(f"{name}: FAIL ({verdict}); if intended, rerun with {hint}")
    if args.update:
        args.baseline.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
