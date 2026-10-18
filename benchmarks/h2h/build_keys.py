"""Build round-5 answer keys with builders that have no server under test.

    python3 benchmarks/h2h/build_keys.py --root ~/data-aggregator-mcp-artifacts/h2h/round5 \
        --ids V1,V2 --parallel 2

Each builder is a fresh headless ``claude -p`` session in its own directory under /tmp
(nothing above it holds a CLAUDE.md or settings), with ``--strict-mcp-config`` and an
empty MCP config (so neither data-aggregator-mcp nor ToolUniverse), no user settings, and
only the Bash, Read and Write tools. Its brief (``<root>/builder_brief.md``) limits it to
the archives' own APIs, DataCite and Europe PMC. Afterwards its transcript is audited
(``audit``): any MCP call, any path outside its own directory, and every host it reached
are recorded in ``<root>/build/<id>/audit.json``; a violation voids the key. A builder
interrupted (rate limit, budget, timeout) is resumed in the same directory from its draft.

Before each start the usage gate reads the newest builder transcript: a five-hour
utilization at or above ``START_BELOW`` waits for the window to reset. Standard library.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

MODEL = "claude-opus-5-5"
START_BELOW = 0.70
TOPICS = {
    "V1": (
        "public spatial transcriptomics datasets (Visium, Slide-seq, MERFISH, Stereo-seq, "
        "GeoMx, seqFISH or similar spatially resolved gene expression) of the inner ear or "
        "cochlea of any vertebrate",
        "spatial transcriptomics of the human placenta",
    ),
    "V2": (
        "chromatin accessibility or chromatin-binding sequencing (ATAC-seq, DNase-seq, "
        "ChIP-seq, CUT&RUN, CUT&Tag) of cnidarians (corals, sea anemones, Hydra, jellyfish)",
        "the same assays in sponges, ctenophores or placozoans",
    ),
    "V3": (
        "brain imaging or recording (MRI, fMRI, DTI, EEG, MEG, fNIRS) of people who stutter",
        "brain imaging of congenitally or early blind people",
    ),
    "V4": (
        "carcass-search or collision-monitoring data for birds or bats at wind turbines",
        "bird collisions with buildings or glass",
    ),
    "V5": (
        "mass-spectrometry or NMR metabolomics or lipidomics of human breast milk",
        "metabolomics or lipidomics of honey",
    ),
    "V6": (
        "ice-core records of black carbon (refractory black carbon, soot)",
        "ice-core records of methanesulfonic acid (MSA)",
    ),
    "V7": (
        "annotated aerial, drone or satellite images of whales made for detection or counting",
        "the same for seals and other pinnipeds",
    ),
    "V8": (
        "speech or voice recordings of people with Parkinson's disease, for acoustic analysis",
        "speech recordings of people with ALS or other dysarthria",
    ),
    "V9": (
        "metagenomic or amplicon (16S/ITS) sequencing of hospital sink drains, P-traps or "
        "other hospital wastewater plumbing",
        "the same for household dishwashers or washing machines",
    ),
    "V10": (
        "ancient DNA sequenced from the dental calculus of non-human animals",
        "ancient DNA from coprolites",
    ),
}
_LOCK = threading.Lock()
_LAST: dict = {"reading": None}


def rate_limit(events: list[dict]) -> dict | None:
    info = None
    for e in events:
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
    if info is None:
        return None
    w = info.get("unifiedWindows") or {}
    return {
        "status": info.get("status"),
        "five_hour": (w.get("five_hour") or {}).get("utilization"),
        "resets": (w.get("five_hour") or {}).get("resetsAt") or info.get("resetsAt"),
        "seven_day": (w.get("seven_day") or {}).get("utilization"),
    }


def events_of(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
_FORBIDDEN = re.compile(
    # "~/" only where a path starts (awk's `$1~/re/` match operator is not a home path).
    r"data-aggregator|data_aggregator|tooluniverse|benchmarks/h2h|\.claude|/home/"
    r"|(?:^|[\s'\"=(])~/",
    re.I,
)


def audit(events: list[dict], work: Path) -> dict:
    """What the builder touched: MCP calls, paths outside its directory, hosts reached."""
    mcp, outside, hosts = [], [], set()
    for e in events:
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        for b in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, text = b.get("name", ""), json.dumps(b.get("input"))
            if name.startswith("mcp__"):
                mcp.append(name)
            hosts.update(h.lower() for h in _HOST.findall(text))
            for m in _FORBIDDEN.finditer(text.replace(str(work), "<work>")):
                outside.append({"tool": name, "match": m.group(0), "input": text[:300]})
    init = next((e for e in events if e.get("subtype") == "init"), {})
    return {
        "mcp_servers": init.get("mcp_servers"),
        "tools": init.get("tools"),
        "mcp_calls": mcp,
        "outside_paths": outside,
        "hosts": sorted(hosts),
        "violation": bool(mcp or outside or init.get("mcp_servers")),
    }


def wait_for_usage() -> None:
    while True:
        with _LOCK:
            rl = _LAST["reading"]
        if not rl or (rl.get("five_hour") or 0) < START_BELOW and rl.get("status") != "rejected":
            return
        until = float(rl.get("resets") or time.time() + 1800) + 60
        print(f"usage gate: five_hour {rl.get('five_hour')}, waiting to {time.ctime(until)}",
              flush=True)  # fmt: skip
        while time.time() < until:
            time.sleep(60)
        with _LOCK:
            _LAST["reading"] = None


def build(task_id: str, root: Path, budget: str, timeout_min: int, max_sessions: int) -> dict:
    primary, backup = TOPICS[task_id]
    work = Path(f"/tmp/h2h-build-{task_id}")  # nosec B108 - fixed name so a resume finds it
    work.mkdir(exist_ok=True)
    shutil.copytree(root / "examples", work / "examples", dirs_exist_ok=True)
    (work / "empty_mcp.json").write_text(json.dumps({"mcpServers": {}}))
    brief = (root / "builder_brief.md").read_text()
    prompt = brief.replace("{ID}", task_id).replace("{PRIMARY}", primary)
    prompt = prompt.replace("{BACKUP}", backup)
    out = root / "build" / task_id
    out.mkdir(parents=True, exist_ok=True)
    report = {"task": task_id, "sessions": []}
    for n in range(1, max_sessions + 1):
        log = out / f"session-{n}.jsonl"
        if log.exists():  # an earlier invocation's session: never overwritten
            continue
        wait_for_usage()
        cmd = [
            "claude", "-p", prompt, "--model", MODEL, "--output-format", "stream-json",
            "--verbose", "--no-session-persistence", "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", str(work / "empty_mcp.json"),
            "--tools", "Bash,Read,Write", "--permission-mode", "bypassPermissions",
            "--max-budget-usd", budget,
        ]  # fmt: skip
        started = time.monotonic()
        with log.open("w") as fh:
            try:
                proc = subprocess.run(
                    cmd, cwd=work, stdout=fh, stderr=subprocess.PIPE, text=True,
                    timeout=timeout_min * 60,
                    env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                )  # fmt: skip
                status = proc.returncode
            except subprocess.TimeoutExpired:
                status = "timeout"
        ev = events_of(log)
        rl = rate_limit(ev)
        with _LOCK:
            _LAST["reading"] = rl or _LAST["reading"]
        result = next((e for e in reversed(ev) if e.get("type") == "result"), {})
        session = {
            "n": n, "exit": status, "wall_s": round(time.monotonic() - started),
            "cost_usd": result.get("total_cost_usd"), "subtype": result.get("subtype"),
            "rate_limit": rl, "audit": audit(ev, work), "reply": (result.get("result") or "")[-1500:],
        }  # fmt: skip
        report["sessions"].append(session)
        key = work / f"{task_id}.yaml"
        if key.exists():
            shutil.copy(key, root / "keys" / f"{task_id}.yaml")
        done = result.get("subtype") == "success" and not result.get("is_error") and key.exists()
        (out / "audit.json").write_text(json.dumps(report, indent=1))
        if done:
            break
        prompt = (
            f"Resume building {task_id}.yaml in this directory: your previous session "
            "stopped before finishing. Read the brief below, your draft and ./scratch/, "
            "and continue.\n\n" + brief.replace("{ID}", task_id)
            .replace("{PRIMARY}", primary).replace("{BACKUP}", backup)
        )  # fmt: skip
    report["violation"] = any(s["audit"]["violation"] for s in report["sessions"])
    report["cost_usd"] = sum(s["cost_usd"] or 0 for s in report["sessions"])
    (out / "audit.json").write_text(json.dumps(report, indent=1))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--budget-usd", default="20")
    ap.add_argument("--timeout-min", type=int, default=150)
    ap.add_argument("--max-sessions", type=int, default=3)
    args = ap.parse_args()
    (args.root / "keys").mkdir(parents=True, exist_ok=True)
    ids = args.ids.split(",")
    with concurrent.futures.ThreadPoolExecutor(min(args.parallel, 2)) as pool:
        futs = {
            pool.submit(build, i, args.root, args.budget_usd, args.timeout_min,
                        args.max_sessions): i
            for i in ids
        }  # fmt: skip
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            print(f"{r['task']}: sessions {len(r['sessions'])}, cost ${r['cost_usd']:.2f}, "
                  f"violation {r['violation']}", flush=True)  # fmt: skip


if __name__ == "__main__":
    main()
