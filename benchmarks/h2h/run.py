"""Head-to-head: the same agent finds and downloads datasets through one MCP server.

Each run is a headless ``claude -p`` session with exactly one arm's tools: ``dam``
(data-aggregator-mcp), ``tu`` (ToolUniverse) or ``web`` (no MCP: web search, web fetch
and a shell). The harness, not the agent, scores the run from the session's stream-json
log and the files it left on disk (see ``score``).

Isolation (round 4 on): every run starts in a fresh ``mktemp`` directory under /tmp
(nothing above it holds a CLAUDE.md or project settings), with ``--setting-sources ""``
(no user, project or local settings, so no hooks or plugins) and the same short
``--system-prompt`` in every arm. Both server arms are pinned PyPI releases started
with ``uvx --exclude-newer``, so their dependencies resolve as of one fixed date.

    python3 benchmarks/h2h/run.py --tasks round4/tasks.json --out <runs> --workers 1
    python3 benchmarks/h2h/run.py --score-only --tasks round4/tasks.json --out <runs>

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
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Rounds 1-3 ran claude-sonnet-5-5 with a $3 cap, ToolUniverse 1.5.4 (rounds 1, 2) and
# 1.5.6 (round 3), and the dam arm from a checkout; their score files are history.
MODEL = "claude-opus-5-5"
DAM_VERSION = "0.74.0"  # latest on PyPI, 2026-10-16
TU_VERSION = "1.5.7"  # latest on PyPI, 2026-10-16
# Dependencies of both servers resolve as of this instant (after both releases).
EXCLUDE_NEWER = "2026-10-17T00:00:00Z"
RUN_TIMEOUT_S = 15 * 60
BUDGET_USD = "6"  # Opus 5.5 costs twice Sonnet 5.5 per token
SYSTEM_PROMPT = (
    "You are a research assistant who finds public scientific datasets. Use only the "
    "tools you have. Work on your own until the task is done: nobody will answer questions."
)

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


def server_spec(arm: str, cfg: argparse.Namespace) -> str | None:
    """What the arm's server is, as recorded in every score row."""
    if arm == "dam":
        return (
            f"checkout:{cfg.dam_dir}" if cfg.dam_dir else f"data-aggregator-mcp=={cfg.dam_version}"
        )
    if arm == "tu":
        return f"tooluniverse=={cfg.tu_version}"
    return None


def mcp_config(arm: str, cfg: argparse.Namespace) -> dict:
    pin = ["--exclude-newer", cfg.exclude_newer]
    if arm == "dam":
        if cfg.dam_dir:  # a checkout under development, not a release: say so in rows
            args = ["run", "--no-sync", "--directory", cfg.dam_dir, "data-aggregator-mcp"]
            return {"mcpServers": {"data-aggregator": {"command": "uv", "args": args}}}
        # The README's `uvx data-aggregator-mcp`, pinned to the release under test.
        server = {"command": "uvx", "args": [*pin, f"data-aggregator-mcp=={cfg.dam_version}"]}
        return {"mcpServers": {"data-aggregator": server}}
    if arm == "tu":
        # ToolUniverse's README configuration, pinned to the release under test.
        server = {
            "command": "uvx",
            "args": [*pin, f"tooluniverse=={cfg.tu_version}"],
            "env": {"PYTHONIOENCODING": "utf-8"},
        }
        return {"mcpServers": {"tooluniverse": server}}
    return {"mcpServers": {}}


def command(
    arm: str,
    prompt: str,
    config: Path,
    writes_list: bool = False,
    *,
    model: str = MODEL,
    budget: str = BUDGET_USD,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        # No user, project or local settings: no hooks, plugins or permission rules.
        "--setting-sources",
        "",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--max-budget-usd",
        budget,
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


def claude_version() -> str:
    proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def run_one(task: dict, arm: str, rep: int, root: Path, cfg: argparse.Namespace) -> Path:
    run_dir = root / arm / task["id"] / f"r{rep}"
    log = run_dir / "session.jsonl"
    if log.exists() and any('"type":"result"' in line for line in log.open()):
        return run_dir  # finished earlier; rerun by deleting the directory
    if run_dir.exists():
        shutil.rmtree(run_dir)  # an unfinished run: start it again from nothing
    run_dir.mkdir(parents=True)
    # A fresh, empty working directory outside any project: no CLAUDE.md above it, no
    # earlier run's files. The answer is copied back into run_dir afterwards.
    cwd = Path(tempfile.mkdtemp(prefix="h2h-", dir="/tmp"))
    out = cwd / "out"
    out.mkdir()
    config = run_dir / "mcp.json"
    config.write_text(json.dumps(mcp_config(arm, cfg)))
    tail = LIST_TAIL if task.get("kind") == "list" else PROMPT_TAIL
    prompt = task["prompt"] + tail.format(out=out)
    started = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                command(
                    arm,
                    prompt,
                    config,
                    task.get("kind") == "list",
                    model=cfg.model,
                    budget=cfg.budget_usd,
                ),
                cwd=cwd,
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
    shutil.copytree(out, run_dir / "out")
    shutil.rmtree(cwd)
    meta = {
        "exit": status,
        "stderr": err,
        "wall_s": time.monotonic() - started,
        "cwd": str(cwd),
        "model_requested": cfg.model,
        "claude_code": cfg.claude_code,
        "server": server_spec(arm, cfg),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
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
    # Rounds 1-3 recorded none of these in meta.json; their init event still names the
    # model and the Claude Code version.
    provenance = {
        "model": init.get("model") or meta.get("model_requested"),
        "claude_code": init.get("claude_code_version") or meta.get("claude_code"),
        "server": meta.get("server"),
    }
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
        **provenance,
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
        "answer": _portable(result.get("result") or "", run_dir, meta.get("cwd"))[:400],
    }


def _portable(answer: str, run_dir: Path, cwd: str | None = None) -> str:
    """``answer`` with the output root (``<out>/<arm>/<task>/r<n>``) written as ``<out>``:
    agents name the file they wrote, and that absolute path is this machine's, not the
    result's. A run in a temporary directory (``cwd``) names that directory instead, and
    its ``out/`` was copied to ``run_dir/out``, so it is written as the run directory."""
    root = run_dir.parents[2]
    if cwd:
        answer = answer.replace(cwd, "<out>/" + str(run_dir.relative_to(root)))
    for form in dict.fromkeys((str(root.resolve()), str(root))):
        answer = answer.replace(form, "<out>")
    return answer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--arms", default="dam,tu,web")
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", "--parallel", type=int, default=1, dest="workers")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--budget-usd", default=BUDGET_USD)
    ap.add_argument("--dam-version", default=DAM_VERSION)
    ap.add_argument("--tu-version", default=TU_VERSION)
    ap.add_argument("--exclude-newer", default=EXCLUDE_NEWER)
    ap.add_argument("--dam-dir", help="run the dam arm from a checkout, not the release")
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
        args.claude_code = claude_version()
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            futures = [pool.submit(run_one, t, a, r, args.out, args) for t, a, r in jobs]
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
                f"{row['task']} {row['arm']:4} {row['run']} exit={row['exit']} "
                f"valid={row['valid']!s:5} json={row['json_ok']!s:5} recall={row['recall']} listed={row['listed']:3} "
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
