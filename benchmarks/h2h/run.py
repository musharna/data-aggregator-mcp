"""Head-to-head: the same agent finds and downloads datasets through one MCP server.

Each run is a headless ``claude -p`` session in an empty directory with exactly one
arm's tools: ``dam`` (data-aggregator-mcp), ``tu`` (ToolUniverse) or ``web`` (no MCP:
web search, web fetch and a shell). The harness, not the agent, scores the run from
the session's stream-json log and the files it left on disk (see ``score``).

    python3 benchmarks/h2h/run.py --tasks tasks.json --out runs/ --dam-dir <checkout>
    python3 benchmarks/h2h/run.py --score-only --tasks tasks.json --out runs/

Standard library only, so it runs without this package's environment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODEL = "claude-sonnet-5-5"
TOOLUNIVERSE = "tooluniverse==1.5.4"
RUN_TIMEOUT_S = 15 * 60
BUDGET_USD = "3"

PROMPT_TAIL = (
    "\n\nSave the file into this directory: {out}\n"
    "When you are done, reply with the identifier (accession, DOI or record id) of the "
    "dataset you found and the path of the file you saved."
)


def mcp_config(arm: str, dam_dir: str | None) -> dict:
    if arm == "dam":
        if not dam_dir:
            sys.exit("--dam-dir is required for the dam arm")
        server = {
            "command": "uv",
            "args": ["run", "--no-sync", "--directory", dam_dir, "data-aggregator-mcp"],
        }
        return {"mcpServers": {"data-aggregator": server}}
    if arm == "tu":
        # ToolUniverse's README configuration, pinned to the release under test.
        server = {
            "command": "uvx",
            "args": [TOOLUNIVERSE],
            "env": {"PYTHONIOENCODING": "utf-8"},
        }
        return {"mcpServers": {"tooluniverse": server}}
    return {"mcpServers": {}}


def command(arm: str, prompt: str, config: Path) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--max-budget-usd",
        BUDGET_USD,
        "--permission-mode",
        "bypassPermissions",
    ]
    if arm == "web":
        cmd += ["--tools", "WebSearch,WebFetch,Bash,Read,Write"]
    else:
        # No built-in tools: the server is the only way to search or download.
        cmd += ["--tools", ""]
    return cmd


def run_one(task: dict, arm: str, rep: int, root: Path, dam_dir: str | None) -> Path:
    run_dir = root / arm / task["id"] / f"r{rep}"
    log = run_dir / "session.jsonl"
    if log.exists() and any('"type":"result"' in line for line in log.open()):
        return run_dir  # finished earlier; rerun by deleting the directory
    out = run_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    config = run_dir / "mcp.json"
    config.write_text(json.dumps(mcp_config(arm, dam_dir)))
    prompt = task["prompt"] + PROMPT_TAIL.format(out=out.resolve())
    started = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                command(arm, prompt, config),
                cwd=run_dir,
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
                timeout=RUN_TIMEOUT_S,
                # One run must not leave notes a later run reads.
                env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
            )
            status = proc.returncode
            err = proc.stderr[-4000:]
        except subprocess.TimeoutExpired:
            status, err = "timeout", ""
    (run_dir / "meta.json").write_text(
        json.dumps({"exit": status, "stderr": err, "wall_s": time.monotonic() - started})
    )
    return run_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def score(task: dict, arm: str, run_dir: Path) -> dict:
    """What the run achieved, read from its log and its directory, not its own claims."""
    events = []
    for line in (run_dir / "session.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    init = next((e for e in events if e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    calls = [
        block["name"]
        for e in events
        if e.get("type") == "assistant"
        for block in e.get("message", {}).get("content", [])
        if block.get("type") == "tool_use"
    ]
    usage = result.get("usage") or {}
    answer = (result.get("result") or "").casefold()
    # The file's sha256, plus any accepted alternative (e.g. the same entry gzipped).
    want = {task["file"]["sha256"], *task["file"].get("also_sha256", [])}
    files = [p for p in run_dir.rglob("*") if p.is_file() and p.name not in
             ("session.jsonl", "meta.json", "mcp.json")]  # fmt: skip
    got = [str(p.relative_to(run_dir)) for p in files if _sha256(p) in want]
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    mcp_calls = [c for c in calls if c.startswith("mcp__")]
    meta = json.loads((run_dir / "meta.json").read_text())
    return {
        "task": task["id"],
        "arm": arm,
        "run": run_dir.name,
        "servers": servers,
        # A server arm that never called its server measured nothing.
        "valid": arm == "web" or bool(mcp_calls),
        "found": any(i.casefold() in answer for i in task["answer_ids"]),
        "bytes_ok": bool(got),
        "files_matching": got,
        "tool_calls": len(calls),
        "tools": calls,
        "turns": result.get("num_turns"),
        "input_tokens": usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": result.get("total_cost_usd"),
        "wall_s": round(meta["wall_s"], 1),
        "exit": meta["exit"],
        "error": result.get("subtype") if result.get("is_error") else None,
        "answer": (result.get("result") or "")[:400],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--arms", default="dam,tu,web")
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--dam-dir")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()
    tasks = json.loads(args.tasks.read_text())
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    arms = args.arms.split(",")
    jobs = [(t, a, r) for r in range(1, args.reps + 1) for t in tasks for a in arms]
    if not args.score_only:
        with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
            futures = [pool.submit(run_one, t, a, r, args.out, args.dam_dir) for t, a, r in jobs]
            for f in concurrent.futures.as_completed(futures):
                print("done", f.result(), flush=True)
    rows = []
    for t, a, r in jobs:
        run_dir = args.out / a / t["id"] / f"r{r}"
        if (run_dir / "meta.json").exists():
            rows.append(score(t, a, run_dir))
    (args.out / "scores.json").write_text(json.dumps(rows, indent=1))
    for row in rows:
        print(
            f"{row['task']} {row['arm']:4} {row['run']} valid={row['valid']!s:5} "
            f"found={row['found']!s:5} bytes={row['bytes_ok']!s:5} calls={row['tool_calls']:3} "
            f"in={row['input_tokens']:>8} out={row['output_tokens']:>6} "
            f"${row['cost_usd'] or 0:.2f} {row['wall_s']}s {row['error'] or ''}"
        )


if __name__ == "__main__":
    main()
