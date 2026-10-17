"""scripts/live_verdict.py, run as the nightly runs it: one process per night."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "live_verdict.py"


def _night(tmp: Path, n: int, down: dict[str, str], previous: Path | None):
    (tmp / f"down{n}.json").write_text(json.dumps(down))
    out = tmp / f"streaks{n}.json"
    cmd = [sys.executable, str(_SCRIPT), str(tmp / f"down{n}.json"), str(out)]
    if previous is not None:
        cmd += ["--previous", str(previous)]
    run = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return run, out


def test_a_test_down_three_runs_in_a_row_fails_the_run_and_one_that_ran_between_does_not(
    tmp_path: Path,
) -> None:
    a, b = "tests/test_a.py::test_a", "tests/test_b.py::test_b"
    one, out1 = _night(tmp_path, 1, {a: "upstream down: A", b: "upstream down: B"}, None)
    two, out2 = _night(tmp_path, 2, {a: "upstream down: A again"}, out1)
    three, out3 = _night(tmp_path, 3, {a: "upstream down: A", b: "upstream down: B"}, out2)
    assert (one.returncode, two.returncode) == (0, 0)  # positive control: no streak yet
    assert json.loads(out2.read_text()) == {a: {"runs": 2, "reason": "upstream down: A again"}}
    assert three.returncode == 1
    assert json.loads(out3.read_text()) == {
        a: {"runs": 3, "reason": "upstream down: A"},
        b: {"runs": 1, "reason": "upstream down: B"},
    }
    gone, rest = three.stdout.split("Down this run only")
    assert a in gone and b not in gone
    assert b in rest


def test_a_run_whose_suite_wrote_no_down_file_fails_loud(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path / "absent.json"), str(tmp_path / "o.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    ok, _ = _night(tmp_path, 1, {}, None)
    assert ok.returncode == 0 and ok.stdout.startswith("Live tests whose upstream")  # control
    assert run.returncode != 0 and "FileNotFoundError" in run.stderr
    assert not (tmp_path / "o.json").exists()
