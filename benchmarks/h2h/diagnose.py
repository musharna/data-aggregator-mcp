"""Where a run's tokens go, per arm: the fixed context, tool traffic, turns and caching.

    python3 benchmarks/h2h/diagnose.py ~/data-aggregator-mcp-artifacts/h2h/r3_runs ...

For every ``<set>/<arm>/<task>/r<n>/session.jsonl`` it reads the init event (tool count),
the first API call's input (system prompt + tool definitions + the task: the fixed
context every turn re-reads), each tool call's input and output size, refused calls
("No such tool available"), and the result's usage (cache writes vs reads, output,
turns, cost). Prints per-arm medians; ``--tsv`` writes one row per run.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

REFUSED = re.compile(r"No such tool available: ([A-Za-z0-9_]+)")


def run_row(path: Path) -> dict:
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    init = next((e for e in events if e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    first = next((e for e in events if e.get("type") == "assistant"), {})
    fu = first.get("message", {}).get("usage", {}) if first else {}
    tool_in, tool_out, refused = 0, 0, []
    by_tool: dict[str, int] = {}
    names: dict[str, str] = {}
    for e in events:
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        for b in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tool_in += len(json.dumps(b.get("input")))
                names[b.get("id", "")] = b.get("name", "")
            elif b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else json.dumps(c)
                tool_out += len(text)
                name = names.get(b.get("tool_use_id", ""), "?")
                by_tool[name] = by_tool.get(name, 0) + len(text)
                refused += REFUSED.findall(text)
    u = result.get("usage") or {}
    tools = init.get("tools", [])
    parts = path.parts
    return {
        "set": parts[-5],
        "arm": parts[-4],
        "task": parts[-3],
        "run": parts[-2],
        "model": init.get("model"),
        "tools_total": len(tools),
        "tools_mcp": sum(1 for t in tools if t.startswith("mcp__")),
        "fixed_ctx": fu.get("input_tokens", 0)
        + fu.get("cache_creation_input_tokens", 0)
        + fu.get("cache_read_input_tokens", 0),
        "turns": result.get("num_turns"),
        "calls": len(names),
        "tool_in_chars": tool_in,
        "tool_out_chars": tool_out,
        "top_out_tool": max(by_tool, key=by_tool.get) if by_tool else "",
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "uncached_in": u.get("input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "cost": result.get("total_cost_usd"),
        "refused": refused,
    }


def med(xs: list) -> float:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="+", type=Path)
    ap.add_argument("--tsv", type=Path)
    args = ap.parse_args()
    rows = [run_row(p) for s in args.sets for p in sorted(s.glob("*/*/r*/session.jsonl"))]
    if args.tsv:
        cols = [k for k in rows[0] if k != "refused"] + ["refused"]
        with args.tsv.open("w") as fh:
            fh.write("\t".join(cols) + "\n")
            for r in rows:
                fh.write(
                    "\t".join(str(r[c]) if c != "refused" else ",".join(r[c]) for c in cols) + "\n"
                )
    keys = ("fixed_ctx", "turns", "calls", "tool_in_chars", "tool_out_chars", "cache_write",
            "cache_read", "output", "cost")  # fmt: skip
    print("set\tarm\tn\ttools(mcp)\t" + "\t".join(keys) + "\trefused_runs\trefused_names")
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["set"], r["arm"]), []).append(r)
    for (s, a), rs in sorted(groups.items()):
        ref = [r for r in rs if r["refused"]]
        names = sorted({n for r in ref for n in r["refused"]})
        vals = "\t".join(f"{med([r[k] for r in rs]):.3g}" for k in keys)
        print(f"{s}\t{a}\t{len(rs)}\t{med([r['tools_total'] for r in rs]):.0f}"
              f"({med([r['tools_mcp'] for r in rs]):.0f})\t{vals}\t{len(ref)}\t{','.join(names)}")  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
