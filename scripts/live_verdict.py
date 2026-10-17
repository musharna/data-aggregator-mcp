"""Count, per live test, the runs in a row its upstream was down; fail at ``--runs``.

The nightly live suite reports a test that failed because a host did not answer as
skipped (``tests/_upstream_down.py``), so one night's outage no longer turns it red.
A host that is gone for good would then never turn it red either, so this carries a
streak per test from the last run to this one, and exits 1 once any test has been
down ``--runs`` runs in a row. A test that ran (passed or failed) drops its streak.

    live_verdict.py DOWN_JSON OUT_JSON [--previous PREV_JSON] [--runs N]

DOWN_JSON is what the plugin wrote: {test id: reason}. OUT_JSON and PREV_JSON are
{test id: {"runs": n, "reason": last reason}}. A Markdown report goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def streaks(down: dict[str, str], previous: dict[str, dict]) -> dict[str, dict]:
    return {
        test: {"runs": previous.get(test, {}).get("runs", 0) + 1, "reason": reason}
        for test, reason in sorted(down.items())
    }


def report(now: dict[str, dict], runs: int) -> tuple[str, bool]:
    gone = [t for t, s in now.items() if s["runs"] >= runs]
    lines = [f"Live tests whose upstream did not answer this run: {len(now)}"]
    if gone:
        lines.append(f"\nDown {runs} runs in a row or more (the job fails on these):\n")
        lines += [f"- `{t}` ({now[t]['runs']} runs): {now[t]['reason']}" for t in gone]
    rest = [t for t in now if t not in gone]
    if rest:
        lines.append("\nDown this run only, or for fewer runs (skipped):\n")
        lines += [f"- `{t}` ({now[t]['runs']}): {now[t]['reason']}" for t in rest]
    return "\n".join(lines) + "\n", bool(gone)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("down", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--previous", type=Path)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    down = json.loads(args.down.read_text())
    previous = json.loads(args.previous.read_text()) if args.previous else {}
    now = streaks(down, previous)
    args.out.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
    text, failed = report(now, args.runs)
    sys.stdout.write(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
