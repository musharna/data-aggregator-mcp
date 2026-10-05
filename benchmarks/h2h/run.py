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
import random
import re
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

# Round 2: "find every dataset" tasks are scored from a list the agent writes.
LIST_TAIL = (
    "\n\nWrite your answer to this file: {out}/datasets.json\n"
    "It must be a JSON list with one object per distinct study: "
    '{{"id": "<main identifier>", "archive": "<archive>", "title": "<title>", '
    '"same_as": ["<identifiers of the same study in other archives>"]}}. '
    "List each study once, with its copies in other archives under same_as.\n"
    "When you are done, reply with the number of studies you listed."
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


def command(arm: str, prompt: str, config: Path, writes_list: bool = False) -> list[str]:
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
        # No built-in tools: the server is the only way to search or download. A list
        # task adds Write, which neither searches nor downloads, for datasets.json.
        cmd += ["--tools", "Write" if writes_list else ""]
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
    tail = LIST_TAIL if task.get("kind") == "list" else PROMPT_TAIL
    prompt = task["prompt"] + tail.format(out=out.resolve())
    started = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                command(arm, prompt, config, task.get("kind") == "list"),
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


def _norm(s: str) -> str:
    s = s.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if s.startswith(prefix):
            return s[len(prefix) :]
    return s


def _aliases(ids: list[str]) -> list[str]:
    """A study's ids, plus the other ways an agent writes a Zenodo record."""
    out = [_norm(i) for i in ids]
    for i in list(out):
        m = re.fullmatch(r"(?:10\.5281/zenodo\.|zenodo:)(\d+)", i)
        if m:
            n = m.group(1)
            out += [
                f"10.5281/zenodo.{n}",
                f"zenodo:{n}",
                f"zenodo.org/records/{n}",
                f"zenodo.org/record/{n}",
            ]
    return sorted(set(out))


def _mentions(text: str, alias: str) -> bool:
    # Whole token only, so GSE12 does not match GSE123.
    return re.search(rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])", text) is not None


def score_list(task: dict, run_dir: Path, adjudication: dict) -> dict:
    """Recall, duplicates and precision of the agent's datasets.json against the key.

    The key is the pre-built studies plus any adjudicated additions (pooling); an entry
    that matches neither the key nor a rejected id is ``pending`` adjudication.
    """
    adj = adjudication.get(task["id"], {})
    # ``alias``: an id adjudication found to be another copy of a study already keyed.
    extra = adj.get("alias", {})
    studies = [
        {**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
        for s in [*task["studies"], *adj.get("add", [])]
    ]
    # Borderline studies: listing one is not wrong, missing one is not a miss.
    optional = [
        {**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
        for s in [*task.get("optional", []), *adj.get("optional", [])]
    ]
    rejected = [_norm(i) for i in adj.get("reject", [])]
    try:
        listed = json.loads((run_dir / "out" / "datasets.json").read_text())
        json_ok = isinstance(listed, list) and all(isinstance(e, dict) for e in listed)
    except (OSError, ValueError):
        listed, json_ok = [], False
    if not json_ok:
        listed = []
    hits: dict[str, int] = {}
    relevant, irrelevant, pending = 0, 0, []
    for entry in listed:
        ids = [entry.get("id"), *(entry.get("same_as") or [])]
        text = " ".join(_norm(str(i)) for i in ids if i)
        matched = [s["key"] for s in studies if any(_mentions(text, a) for a in _aliases(s["ids"]))]
        for key in matched:
            hits[key] = hits.get(key, 0) + 1
        if matched or any(any(_mentions(text, a) for a in _aliases(s["ids"])) for s in optional):
            relevant += 1
        elif any(_mentions(text, r) for r in rejected):
            irrelevant += 1
        else:
            pending.append({"id": entry.get("id"), "same_as": entry.get("same_as") or [],
                            "title": entry.get("title")})  # fmt: skip
    archives = sorted({str(e.get("archive", "")).strip() for e in listed} - {""})
    return {
        "json_ok": json_ok,
        "listed": len(listed),
        "key_size": len(studies),
        "found_keys": sorted(hits),
        "recall": round(len(hits) / len(studies), 3) if studies else None,
        # A study listed as two or more separate entries.
        "duplicates": sum(1 for n in hits.values() if n > 1),
        "relevant": relevant,
        "irrelevant": irrelevant,
        "pending": pending,
        "precision": round(relevant / (relevant + irrelevant), 3)
        if relevant + irrelevant
        else None,
        "archives_listed": archives,
    }


def _result_sources(events: list[dict]) -> list[str]:
    """Distinct ``source`` values in this server's tool results: did the fan-out fire?"""
    found: set[str] = set()
    for e in events:
        if e.get("type") != "user":
            continue
        for block in e.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = json.dumps(content) if not isinstance(content, str) else content
            found.update(re.findall(r'\\?"source\\?":\s*\\?"([a-z_]+)', text))
    return sorted(found)


def score(task: dict, arm: str, run_dir: Path, adjudication: dict | None = None) -> dict:
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
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    mcp_calls = [c for c in calls if c.startswith("mcp__")]
    meta = json.loads((run_dir / "meta.json").read_text())
    if task.get("kind") == "list":
        outcome = score_list(task, run_dir, adjudication or {})
        outcome["result_sources"] = _result_sources(events)
    else:
        # The file's sha256, plus any accepted alternative (e.g. the same entry gzipped).
        want = {task["file"]["sha256"], *task["file"].get("also_sha256", [])}
        files = [p for p in run_dir.rglob("*") if p.is_file() and p.name not in
                 ("session.jsonl", "meta.json", "mcp.json")]  # fmt: skip
        got = [str(p.relative_to(run_dir)) for p in files if _sha256(p) in want]
        outcome = {
            "found": any(i.casefold() in answer for i in task["answer_ids"]),
            "bytes_ok": bool(got),
            "files_matching": got,
        }
    return {
        "task": task["id"],
        "arm": arm,
        "run": run_dir.name,
        "servers": servers,
        # A server arm that never called its server measured nothing.
        "valid": arm == "web" or bool(mcp_calls),
        **outcome,
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
        "answer": _portable(result.get("result") or "", run_dir)[:400],
    }


def _portable(answer: str, run_dir: Path) -> str:
    """``answer`` with the output root (``<out>/<arm>/<task>/r<n>``) written as ``<out>``:
    agents name the file they wrote, and that absolute path is this machine's, not the
    result's."""
    root = run_dir.parents[2]
    for form in dict.fromkeys((str(root.resolve()), str(root))):
        answer = answer.replace(form, "<out>")
    return answer


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
    ap.add_argument("--adjudication", type=Path, help="list tasks: pooled additions/rejects")
    args = ap.parse_args()
    tasks = json.loads(args.tasks.read_text())
    adjudication = json.loads(args.adjudication.read_text()) if args.adjudication else {}
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
            rows.append(score(t, a, run_dir, adjudication))
    (args.out / "scores.json").write_text(json.dumps(rows, indent=1))
    # Pooling: every listed entry no key study or reject covers, with no arm or run
    # attached and in a shuffled order, so adjudication cannot favour an arm.
    pool: dict[str, dict[str, dict]] = {}
    for row in rows:
        for entry in row.get("pending", []):
            pool.setdefault(row["task"], {}).setdefault(_norm(str(entry["id"])), entry)
    shuffled = {}
    for task_id, entries in sorted(pool.items()):
        items = list(entries.values())
        random.Random(task_id).shuffle(items)
        shuffled[task_id] = items
    (args.out / "pool.json").write_text(json.dumps(shuffled, indent=1))
    for row in rows:
        if "recall" in row:
            print(
                f"{row['task']} {row['arm']:4} {row['run']} valid={row['valid']!s:5} "
                f"json={row['json_ok']!s:5} recall={row['recall']} listed={row['listed']:3} "
                f"dup={row['duplicates']} prec={row['precision']} pending={len(row['pending'])} "
                f"src={len(row['result_sources'])} calls={row['tool_calls']:3} "
                f"in={row['input_tokens']:>8} ${row['cost_usd'] or 0:.2f} {row['wall_s']}s "
                f"{row['error'] or ''}"
            )
            continue
        print(
            f"{row['task']} {row['arm']:4} {row['run']} valid={row['valid']!s:5} "
            f"found={row['found']!s:5} bytes={row['bytes_ok']!s:5} calls={row['tool_calls']:3} "
            f"in={row['input_tokens']:>8} out={row['output_tokens']:>6} "
            f"${row['cost_usd'] or 0:.2f} {row['wall_s']}s {row['error'] or ''}"
        )


if __name__ == "__main__":
    main()
