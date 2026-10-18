"""Blind adjudication of a sweep's pooled entries by agents that built no key and ran no arm.

    python3 benchmarks/h2h/adjudicate.py --keys benchmarks/h2h/round4/keys \
        --pool <runs>/pool.json --out <runs>/adjudication

For each task one fresh headless ``claude -p`` session (in /tmp, ``--strict-mcp-config``
with no MCP server, no user settings, tools Bash/Read/Write) gets the task's prompt and
criteria, the keyed studies' ids and titles, the near misses, and the pooled entries
(``pool.json``: no arm, no run, shuffled). It checks each entry through the archives' own
APIs and writes one verdict per entry: ``alias:S<n>`` (another id of a keyed study),
``add:A<n>`` (a new qualifying study; entries naming one study share its label),
``optional:B<n>`` (borderline) or ``reject``, with a reason. ``merge`` turns the verdicts
into ``adjudication.json`` (the scorer's format) and ``adjudication_reasons.yaml``.
Its transcript is audited like a key builder's. Standard library plus PyYAML for the keys.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_keys import audit, events_of, rate_limit  # noqa: E402

MODEL = "claude-opus-5-5"
BRIEF = """You are adjudicating entries for a benchmark of agents that search for public
datasets. Agents were given the task below and listed studies; the entries in
`entries.json` are ones the answer key does not cover. Nobody knows which agent or
system listed which entry, and it must not matter.

Your working directory holds `task.json`: the task prompt, the criteria the key builder
applied, the keyed studies (key, title, ids), borderline studies and near misses, and
`entries.json`: the entries to judge (id, same_as, title as the agent wrote them).

For EVERY entry, check it through the archive's own public API (curl; DataCite, doi.org,
Europe PMC, NCBI, ENA, Zenodo, Figshare, Dryad, OSF, Dataverse, PANGAEA, OpenNeuro, DANDI
and so on). Do not use any search engine or AI tool, read nothing on this machine outside
this directory, and keep NCBI under 3 requests/s and other hosts at 1 request/s.

Verdicts, applying the task's criteria exactly as the key builder did:
- `alias:S<n>`: another id, version, mirror or copy of keyed (or borderline) study S<n>.
- `add:A<n>`: a public deposit that meets the criteria and is not in the key. Entries
  that name the same new study share one label (A1, A2...).
- `optional:B<n>`: borderline by the criteria's own borderline rules.
- `reject`: does not qualify (wrong topic, no public data, a paper not a dataset, does
  not resolve, a near miss...).

Write `verdicts.json`: a JSON list with one object per entry, in the order given:
{"id": <the entry's id, verbatim>, "verdict": "...", "reason": "<one or two sentences
naming what you checked>"}. Write it early and update it as you go. Reply "done" when
every entry has a verdict.
"""


def judge(task: dict, entries: list[dict], out: Path, budget: str, timeout_min: int) -> dict:
    tid = task["task"]
    work = Path(f"/tmp/h2h-adj-{out.name}-{tid}")  # nosec B108 - fresh dir outside any project
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    (work / "task.json").write_text(json.dumps(task, indent=1))
    (work / "entries.json").write_text(json.dumps(entries, indent=1))
    (work / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    log = out / f"{tid}.session.jsonl"
    cmd = [
        "claude", "-p", BRIEF, "--model", MODEL, "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
        "--mcp-config", str(work / "mcp.json"), "--tools", "Bash,Read,Write",
        "--permission-mode", "bypassPermissions", "--max-budget-usd", budget,
    ]  # fmt: skip
    with log.open("w") as fh:
        try:
            proc = subprocess.run(cmd, cwd=work, stdout=fh, stderr=subprocess.PIPE, text=True,
                                  timeout=timeout_min * 60,
                                  env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})  # fmt: skip
            status = proc.returncode
        except subprocess.TimeoutExpired:
            status = "timeout"
    ev = events_of(log)
    result = next((e for e in reversed(ev) if e.get("type") == "result"), {})
    verdicts = (
        json.loads((work / "verdicts.json").read_text())
        if (work / "verdicts.json").exists()
        else []
    )
    # Only a complete set is kept as final; a partial one is kept aside and re-judged.
    complete = {v.get("id") for v in verdicts} >= {e["id"] for e in entries}
    name = f"{tid}.verdicts.json" if complete else f"{tid}.verdicts.partial.json"
    (out / name).write_text(json.dumps(verdicts, indent=1))
    rep = {"task": tid, "exit": status, "entries": len(entries), "verdicts": len(verdicts),
           "cost_usd": result.get("total_cost_usd"), "rate_limit": rate_limit(ev),
           "audit": audit(ev, work)}  # fmt: skip
    shutil.rmtree(work)
    return rep


def merge(verdicts: dict[str, list[dict]], pool: dict[str, list[dict]]) -> tuple[dict, list]:
    """Verdicts -> the scorer's adjudication (alias / add / optional / reject per task)."""
    adj: dict[str, dict] = {}
    reasons = []
    for tid, entries in pool.items():
        by_id = {v["id"]: v for v in verdicts.get(tid, [])}
        a: dict = {"add": [], "optional": [], "reject": [], "alias": {}}
        groups: dict[str, list[str]] = {}
        for e in entries:
            v = by_id.get(e["id"])
            if v is None:
                raise SystemExit(f"{tid}: no verdict for {e['id']}")
            ids = [str(e["id"]), *[str(x) for x in e.get("same_as") or []]]
            kind, _, label = v["verdict"].partition(":")
            if kind == "alias":
                a["alias"].setdefault(label, []).extend(ids)
            elif kind in ("add", "optional"):
                groups.setdefault(f"{kind}:{label}", []).extend(ids)
            elif kind == "reject":
                a["reject"].append(str(e["id"]))
            else:
                raise SystemExit(f"{tid}: bad verdict {v['verdict']!r} for {e['id']}")
            reasons.append({"task": tid, "id": e["id"], "verdict": v["verdict"],
                            "reason": v.get("reason", "")})  # fmt: skip
        for g, ids in sorted(groups.items()):
            kind, label = g.split(":")
            a[kind].append({"key": label, "ids": sorted(set(ids))})
        adj[tid] = a
    return adj, reasons


def task_view(key: dict) -> dict:
    return {
        "task": key["task"],
        "prompt": key["prompt"],
        "criteria": key.get("criteria"),
        "studies": [
            {
                "key": s["key"],
                "title": s.get("title"),
                "ids": s["ids"],
                "borderline": str(s.get("evidence", "")).startswith("confidence: borderline"),
            }
            for s in key["studies"]
        ],  # fmt: skip
        "near_misses": key.get("near_misses", []),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", required=True, type=Path)
    ap.add_argument("--pool", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default="")
    ap.add_argument("--budget-usd", default="10")
    ap.add_argument("--timeout-min", type=int, default=90)
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args()
    pool = json.loads(args.pool.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    tids = [t for t in sorted(pool) if not args.only or t in args.only.split(",")]
    if not args.merge_only:
        for tid in tids:
            if (args.out / f"{tid}.verdicts.json").exists():
                continue
            key = yaml.safe_load((args.keys / f"{tid}.yaml").read_text())
            rep = judge(task_view(key), pool[tid], args.out, args.budget_usd, args.timeout_min)
            (args.out / f"{tid}.report.json").write_text(json.dumps(rep, indent=1))
            print(f"{tid}: {rep['verdicts']}/{rep['entries']} verdicts, ${rep['cost_usd']}, "
                  f"violation {rep['audit']['violation']}", flush=True)  # fmt: skip
            time.sleep(1)
    verdicts = {t: json.loads((args.out / f"{t}.verdicts.json").read_text()) for t in sorted(pool)
                if (args.out / f"{t}.verdicts.json").exists()}  # fmt: skip
    adj, reasons = merge(verdicts, {t: pool[t] for t in verdicts})
    (args.out / "adjudication.json").write_text(json.dumps(adj, indent=1) + "\n")
    with (args.out / "adjudication_reasons.yaml").open("w") as fh:
        fh.write("# Blind adjudication by fresh agents that built no key and ran no arm\n")
        fh.writelines(f"- {json.dumps(r)}\n" for r in reasons)


if __name__ == "__main__":
    main()
