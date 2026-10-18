"""Build a round's ``tasks.json`` from its answer keys (``keys/*.yaml``).

    uv run python benchmarks/h2h/build_tasks.py benchmarks/h2h/round4 --frozen 2026-10-16

A keyed study marked ``confidence: borderline`` (a field, or the start of its evidence)
goes to ``optional`` (listing it is not wrong, missing it is not a miss); every other
study is scored. The prompt is the key's ``prompt``, whitespace collapsed. ``--frozen`` stamps the date the keys were
committed, before any run. Rebuilding round 3 from its keys reproduces its tasks.json
(tests/test_h2h_score.py).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

BORDERLINE = "confidence: borderline"


def build(round_dir: Path, frozen: str | None = None) -> list[dict]:
    tasks = []
    for path in sorted((round_dir / "keys").glob("*.yaml")):
        key = yaml.safe_load(path.read_text())
        firm, optional = [], []
        for s in key["studies"]:
            entry = {"key": s["key"], "ids": [str(i) for i in s["ids"]]}
            borderline = s.get("confidence") == "borderline" or str(
                s.get("evidence", "")
            ).strip().startswith(BORDERLINE)
            (optional if borderline else firm).append(entry)
        task = {"id": key["task"], "kind": "list", "prompt": " ".join(key["prompt"].split())}
        if frozen:
            task["frozen"] = frozen
        tasks.append({**task, "studies": firm, "optional": optional})
    return tasks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("round_dir", type=Path)
    ap.add_argument("--frozen", help="date the keys were frozen (YYYY-MM-DD)")
    args = ap.parse_args()
    tasks = build(args.round_dir, args.frozen)
    (args.round_dir / "tasks.json").write_text(json.dumps(tasks, indent=1) + "\n")
    for t in tasks:
        print(t["id"], "studies", len(t["studies"]), "optional", len(t["optional"]))


if __name__ == "__main__":
    main()
