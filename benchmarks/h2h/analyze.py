"""Controls and statistics for the head-to-head list rounds: rescore, baselines, intervals.

For each run set (a directory of ``<arm>/<task>/r<n>/`` runs, kept outside the repo):

- rescore every run with ``run.score`` (the current scorer, validity rule and leak scan);
- per-arm accuracy (mean over tasks of the task's mean recall; an invalid run scores 0),
  cost, wall time and tool calls, each with a bootstrap 95% interval over tasks;
- paired per-task comparisons (dam vs web, dam vs tu, and every arm vs bare where it ran):
  wins, losses, ties, an exact two-sided sign test, and the bootstrap interval of the
  mean difference;
- baselines scored by the same scorer with no model: an empty list, a constant answer
  (the same list for every task: every other task's key) and a naive shortcut (DataCite's
  top results for a fixed keyword query, fetched once and kept in ``baselines/``).

    python3 benchmarks/h2h/analyze.py --artifacts ~/data-aggregator-mcp-artifacts/h2h

Writes ``summary.tsv`` and ``comparisons.tsv`` here and one ``scores_rescored.json`` per
set under ``--write-to``. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run  # noqa: E402

# name: (round dir, run-set dir under --artifacts, runs per arm and task, what it ran)
SETS = {
    "r2_first": ("round2", "r2_runs", 2, "round 2 first run, dam 0.60.0, 2026-10-05"),
    "r2_rerun": ("round2", "r2_rerun", 2, "round 2 after #244/#245, 2026-10-05"),
    "r2_066": ("round2", "r2_066", 2, "round 2 on dam 0.66.0, 2026-10-06"),
    "r2_0671": ("round2", "r2_0671", 2, "round 2 on dam 0.67.1, 2026-10-07"),
    "r2_266": ("round2", "r2_266_3arm", 2, "round 2 on dam #266, 2026-10-07"),
    "r2_final": ("round2", "r2_0691", 2, "round 2 final, dam 0.69.1, 2026-10-07"),
    "r3_heldout": ("round3", "r3_runs", 2, "round 3 held out, dam 0.69.1, 2026-10-11"),
    "r3_071": ("round3", "r3_071", 2, "round 3 rerun, dam 0.71.0 (after tuning on it)"),
    "r4_pilot": ("round4", "r4_pilot", 1, "round 4 pilot, U2 only, Opus 5.5, dam 0.74.0"),
    "r4": ("round4", "r4_runs", 3, "round 4 held out, Opus 5.5, dam 0.74.0, 2026-10-18"),
    "r5": ("round5", "r5_runs", 3, "round 5 held out, Opus 5.5, dam 0e3661f (unreleased)"),
    "r5_haiku": ("round5", "r5_haiku_runs", 3, "round 5 on Haiku 5.5, dam 0.75.4, 2026-10-20"),
}
# A set scored with another adjudication than its round's ``adjudication.json``: the
# Haiku sweep's file is round 5's verdicts plus those on the Haiku runs' new entries.
SET_ADJUDICATION = {"r5_haiku": "round5/adjudication_haiku_2026-10-20.json"}
# Non-concurrent comparisons (PREREG_2026-10-20_model-tiers.md): an arm of one set against
# an arm of another on the same tasks, both scored with the first set's adjudication.
CROSS = (("r5_haiku", "dam", "r5", "web"), ("r5_haiku", "dam", "r5", "dam"))
ARMS = ("dam", "tu", "web", "bare")
PAIRS = (("dam", "web"), ("dam", "tu"), ("tu", "web"), ("dam", "bare"), ("web", "bare"))
BOOT = 10_000

# The naive shortcut's query per task, written from the prompt's subject before any fetch.
DATACITE_QUERY = {
    "R1": "Phelipanche aegyptiaca",
    "R2": "axolotl limb regeneration single-cell",
    "R3": "tardigrade desiccation",
    "R4": "snow leopard",
    "R5": "Chlamydomonas nitrogen proteomics",
    "T1": "olfactory bulb odor",
    "T2": "tendon single-cell RNA-seq",
    "T3": "thermokarst lake methane",
    "T4": "honey bee images",
    "T5": "coral metabolomics",
    "U1": "intracranial sleep",
    "U2": "Batrachochytrium salamandrivorans",
    "U3": "rice drought proteomics",
    "U4": "microplastics snow ice",
    "U5": "eye-tracking children reading",
    # Round 5: the queries PREREG_2026-10-18.md fixed before any fetch (backup topics used
    # for V4, V5 and V6, whose primaries passed the 30-study cap).
    "V1": "spatial transcriptomics cochlea",
    "V2": "cnidarian ATAC-seq",
    "V3": "stuttering MRI",
    "V4": "bird window collisions",
    "V5": "honey metabolomics",
    "V6": "ice core methanesulfonic acid",
    "V7": "whale aerial imagery detection",
    "V8": "Parkinson speech recordings",
    "V9": "hospital sink drain microbiome",
    "V10": "dental calculus ancient DNA animal",
}
DATACITE_TOP = 100


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test on the non-tied pairs."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def interval(values_by_task: dict[str, list[float]], stat, seed: int = 0) -> tuple:
    """``stat`` over all values, with a percentile bootstrap 95% interval that resamples
    tasks (a task's runs travel together)."""
    tasks = sorted(values_by_task)
    every = [v for t in tasks for v in values_by_task[t]]
    if not every:
        return (None, None, None)
    rng = random.Random(seed)
    boots = []
    for _ in range(BOOT):
        pick = [rng.choice(tasks) for _ in tasks]
        boots.append(stat([v for t in pick for v in values_by_task[t]], pick))
    boots.sort()
    return (stat(every, tasks), boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1])


def task_means(rows: list[dict], arm: str, key: str) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for r in rows:
        if r["arm"] == arm:
            by.setdefault(r["task"], []).append(_value(r, key))
    return {t: statistics.fmean(v) for t, v in by.items()}


def _value(row: dict, key: str) -> float:
    if key == "recall":  # an invalid run delivered nothing the arm can be credited with
        return (row["recall"] or 0.0) if row["valid"] else 0.0
    if key == "precision_listed":  # unjudged entries count as wrong, for every row alike
        return row["relevant"] / row["listed"] if row["listed"] else 0.0
    if key == "calls":
        return float(row["tool_calls"])
    if key == "cost":
        return float(row["cost_usd"] or 0.0)
    return float(row[key])


def _mean_of_task_means(values: list[float], _tasks) -> float:
    return statistics.fmean(values)


def _median(values: list[float], _tasks) -> float:
    return statistics.median(values)


def summarize(name: str, rows: list[dict]) -> list[dict]:
    out = []
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        if not mine:
            continue
        rec = {t: [m] for t, m in task_means(rows, arm, "recall").items()}
        prec = {t: [m] for t, m in task_means(rows, arm, "precision_listed").items()}
        # Sensitivity: the valid runs only (a task with none drops out).
        ok = task_means([r for r in mine if r["valid"]], arm, "recall")
        raw: dict[str, dict[str, list[float]]] = {"cost": {}, "wall_s": {}, "calls": {}}
        for r in mine:
            for k in raw:
                raw[k].setdefault(r["task"], []).append(_value(r, k))
        row = {
            "set": name,
            "arm": arm,
            "tasks": len(rec),
            "runs": len(mine),
            "valid": sum(r["valid"] for r in mine),
            "invalid": sum(not r["valid"] for r in mine),
            "leak_runs": sum(bool(r["leaks"]) for r in mine),
            "unjudged_entries": sum(len(r["pending"]) for r in mine),
        }
        for label, data, stat in (
            ("recall", rec, _mean_of_task_means),
            ("recall_valid_only", {t: [m] for t, m in ok.items()}, _mean_of_task_means),
            ("precision_listed", prec, _mean_of_task_means),
            ("cost_usd_median", raw["cost"], _median),
            ("wall_s_median", raw["wall_s"], _median),
            ("calls_median", raw["calls"], _median),
        ):
            est, lo, hi = interval(data, stat)
            row |= {label: est, f"{label}_lo": lo, f"{label}_hi": hi}
        out.append(row)
    return out


def compare(name: str, rows: list[dict]) -> list[dict]:
    out = []
    for a, b in PAIRS:
        for metric in ("recall", "recall_valid_only", "precision_listed", "cost", "wall_s",
                       "calls"):  # fmt: skip
            if metric == "recall_valid_only":  # the PREREG's agreement check: valid runs
                ok = [r for r in rows if r["valid"]]  # only; a task either arm lacks drops
                ma, mb = task_means(ok, a, "recall"), task_means(ok, b, "recall")
            else:
                ma, mb = task_means(rows, a, metric), task_means(rows, b, metric)
            both = sorted(set(ma) & set(mb))
            if not both:
                continue
            diffs = {t: [ma[t] - mb[t]] for t in both}
            wins = sum(d[0] > 1e-9 for d in diffs.values())
            losses = sum(d[0] < -1e-9 for d in diffs.values())
            est, lo, hi = interval(diffs, _mean_of_task_means)
            out.append({
                "set": name, "a": a, "b": b, "metric": metric, "tasks": len(both),
                "a_higher": wins, "b_higher": losses, "ties": len(both) - wins - losses,
                "sign_p": sign_test(wins, losses), "mean_diff": est,
                "mean_diff_lo": lo, "mean_diff_hi": hi,
            })  # fmt: skip
    return out


def rescore(round_dir: Path, run_dir: Path, reps: int, adj_path: Path | None = None) -> list[dict]:
    tasks = json.loads((round_dir / "tasks.json").read_text())
    adj_path = adj_path or round_dir / "adjudication.json"
    adj = json.loads(adj_path.read_text()) if adj_path.exists() else {}
    rows = []
    for t in tasks:
        for arm in ARMS:
            for r in range(1, reps + 1):
                d = run_dir / arm / t["id"] / f"r{r}"
                if (d / "meta.json").exists():
                    rows.append(run.score(t, arm, d, adj))
    return rows


def fetch_datacite(path: Path) -> dict:
    """The naive shortcut's results, fetched once (2 s between requests)."""
    # A task already fetched keeps its results (rounds 2 to 4 are from 2026-10-16); only
    # new tasks are fetched, each stamped with its own date.
    out = (
        json.loads(path.read_text())
        if path.exists()
        else {"fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "top": DATACITE_TOP, "tasks": {}}
    )
    for task, query in DATACITE_QUERY.items():
        if task in out["tasks"]:
            continue
        url = "https://api.datacite.org/dois?" + urllib.parse.urlencode({
            "query": query, "resource-type-id": "dataset",
            "page[size]": DATACITE_TOP, "fields[dois]": "doi",
        })  # fmt: skip
        req = urllib.request.Request(url, headers={"User-Agent": "h2h-baseline/1"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - fixed https host
            body = json.load(resp)
        out["tasks"][task] = {
            "query": query, "url": url, "dois": [d["attributes"]["doi"] for d in body["data"]],
            "total": body.get("meta", {}).get("total"),
            "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }  # fmt: skip
        time.sleep(2)
    path.write_text(json.dumps(out, indent=1) + "\n")
    return out


def baselines(round_name: str, datacite: dict) -> list[dict]:
    """Model-free rows through the real scorer, one per task and baseline."""
    round_dir = HERE / round_name
    tasks = [t for t in json.loads((round_dir / "tasks.json").read_text()) if t["kind"] == "list"]
    adj_path = round_dir / "adjudication.json"
    adj = json.loads(adj_path.read_text()) if adj_path.exists() else {}
    rows = []
    for t in tasks:
        a = adj.get(t["id"], {})
        extra = a.get("alias", {})
        studies = [{**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
                   for s in [*t["studies"], *a.get("add", [])]]  # fmt: skip
        optional = [{**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
                    for s in [*t.get("optional", []), *a.get("optional", [])]]  # fmt: skip
        rejected = [run._norm(i) for i in a.get("reject", [])]
        others = [{"id": s["ids"][0], "same_as": s["ids"][1:]}
                  for o in tasks if o["id"] != t["id"] for s in o["studies"]]  # fmt: skip
        dois = datacite["tasks"].get(t["id"], {}).get("dois", [])
        for arm, listed in (
            ("base_empty", []),
            ("base_constant", others),
            ("base_datacite25", [{"id": d} for d in dois[:25]]),
            (f"base_datacite{DATACITE_TOP}", [{"id": d} for d in dois]),
        ):
            s = run.score_entries(listed, studies, optional, rejected)
            rows.append({
                **s, "task": t["id"], "arm": arm, "valid": True, "leaks": [],
                "tool_calls": 0, "cost_usd": 0.0, "wall_s": 0.0,
            })  # fmt: skip
    return rows


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def write_tsv(path: Path, rows: list[dict]) -> None:
    cols = list(dict.fromkeys(k for r in rows for k in r))
    lines = ["\t".join(cols)] + ["\t".join(_fmt(r.get(c)) for c in cols) for r in rows]
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, type=Path)
    ap.add_argument("--write-to", type=Path, help="rescored rows per set (default: none)")
    ap.add_argument("--fetch-datacite", action="store_true", help="refetch the shortcut")
    args = ap.parse_args()
    cache = HERE / "baselines" / "datacite.json"
    datacite = fetch_datacite(cache) if args.fetch_datacite else json.loads(cache.read_text())
    summary, comparisons = [], []
    for name, (round_name, set_dir, reps, about) in SETS.items():
        run_dir = args.artifacts / set_dir
        if not run_dir.exists():
            print(f"{name}: {run_dir} missing, skipped", file=sys.stderr)
            continue
        adj = HERE / SET_ADJUDICATION[name] if name in SET_ADJUDICATION else None
        rows = rescore(HERE / round_name, run_dir, reps, adj)
        if args.write_to:
            (args.write_to / name).mkdir(parents=True, exist_ok=True)
            (args.write_to / name / "scores_rescored.json").write_text(
                json.dumps(rows, indent=1) + "\n"
            )
        for s in summarize(name, rows):
            summary.append({**s, "about": about})
        comparisons += compare(name, rows)
    for set_a, arm_a, set_b, arm_b in CROSS:
        round_name, dir_a, reps, _ = SETS[set_a]
        dir_b = SETS[set_b][1]
        if not ((args.artifacts / dir_a).exists() and (args.artifacts / dir_b).exists()):
            continue
        adj = HERE / SET_ADJUDICATION[set_a] if set_a in SET_ADJUDICATION else None
        # Relabelled "dam" (set a) and "web" (set b) so compare() pairs them; the set name
        # says which arm of which set each one is.
        a = [{**r, "arm": "dam"} for r in rescore(HERE / round_name, args.artifacts / dir_a,
                                                  reps, adj) if r["arm"] == arm_a]  # fmt: skip
        b = [{**r, "arm": "web"} for r in rescore(HERE / round_name, args.artifacts / dir_b,
                                                  SETS[set_b][2], adj) if r["arm"] == arm_b]  # fmt: skip
        name = f"{set_a}:{arm_a}_vs_{set_b}:{arm_b}"
        comparisons += [c for c in compare(name, a + b) if (c["a"], c["b"]) == ("dam", "web")]
    for round_name in ("round2", "round3", "round4", "round5"):
        if not (HERE / round_name / "tasks.json").exists():  # round 5's lands after its sweep
            print(f"{round_name}: no tasks.json yet, baselines skipped", file=sys.stderr)
            continue
        rows = baselines(round_name, datacite)
        for arm in sorted({r["arm"] for r in rows}):
            mine = [r for r in rows if r["arm"] == arm]
            rec = {r["task"]: [r["recall"]] for r in mine}
            prec = {r["task"]: [_value(r, "precision_listed")] for r in mine}
            est, lo, hi = interval(rec, _mean_of_task_means)
            pe, plo, phi = interval(prec, _mean_of_task_means)
            summary.append({
                "set": round_name, "arm": arm, "tasks": len(mine), "runs": len(mine),
                "valid": len(mine), "invalid": 0, "leak_runs": 0,
                "unjudged_entries": sum(len(r["pending"]) for r in mine),
                "recall": est, "recall_lo": lo, "recall_hi": hi,
                "precision_listed": pe, "precision_listed_lo": plo, "precision_listed_hi": phi,
                "about": "no model: scorer only",
            })  # fmt: skip
    write_tsv(HERE / "summary.tsv", summary)
    write_tsv(HERE / "comparisons.tsv", comparisons)
    for s in summary:
        print(
            f"{s['set']:10} {s['arm']:16} n={s['runs']:2} valid={s['valid']:2} "
            f"leak={s['leak_runs']} recall={_fmt(s['recall'])} "
            f"[{_fmt(s['recall_lo'])}, {_fmt(s['recall_hi'])}] "
            f"cost={_fmt(s.get('cost_usd_median'))} unjudged={s['unjudged_entries']}"
        )


if __name__ == "__main__":
    main()
