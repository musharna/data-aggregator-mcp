"""The perf ratchet's own logic (``benchmarks/perf``): the gate itself runs as a CI step.

Seen to fail: a mutant adding one ``_year(None)`` call to ``datacite._normalize`` turned
the gate red on search_fanout (+40), cursor_paging (+68) and resolve (+1), each naming
``datacite.py:_year``, while fetch (no DataCite) stayed equal (PR body has the run).
``--update`` tests seen to fail under ``mutate-run``: a rise accepted without
``--allow-increase``, and a changed output accepted with ``--allow-increase`` alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import httpx
import pytest

PERF = Path(__file__).resolve().parents[1] / "benchmarks" / "perf"
sys.path.insert(0, str(PERF))
import ratchet  # noqa: E402
import upstream  # noqa: E402


def _row(calls: dict[str, int], http: dict[str, int], output: dict | None = None) -> dict:
    return {
        "calls_total": sum(calls.values()),
        "http_total": sum(http.values()),
        "http": http,
        "calls": calls,
        "output": output or {"n": 1},
    }


def test_compare_equal_rise_drop_and_output() -> None:
    base = _row({"a.py:f": 3, "a.py:g": 1}, {"h": 2})
    assert ratchet.compare("w", base, _row({"a.py:f": 3, "a.py:g": 1}, {"h": 2}))[0] == "equal"
    verdict, lines = ratchet.compare("w", base, _row({"a.py:f": 4, "a.py:g": 1}, {"h": 2}))
    assert verdict == "higher"
    assert "    +1  a.py:f  (3 -> 4)" in lines
    # Same call total, one more request: still a rise.
    assert ratchet.compare("w", base, _row({"a.py:f": 3, "a.py:g": 1}, {"h": 3}))[0] == "higher"
    assert ratchet.compare("w", base, _row({"a.py:f": 2, "a.py:g": 1}, {"h": 2}))[0] == "lower"
    # The cheapest possible run is the one that did nothing: an output change is never
    # read as a drop.
    assert ratchet.compare("w", base, _row({}, {}, {"n": 0}))[0] == "output"
    assert ratchet.compare("w", None, base)[0] == "new"


def test_gate_fails_on_drop_without_update_and_on_rise_without_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline.json"
    rows = {"w": _row({"a.py:f": 3}, {"h": 1})}
    monkeypatch.setattr(ratchet, "WORKLOADS", {"w": None})
    monkeypatch.setattr(ratchet, "measure", lambda name: rows[name])
    baseline.write_text(ratchet.json.dumps({"w": _row({"a.py:f": 3}, {"h": 1})}))
    assert ratchet.main(["--baseline", str(baseline)]) == 0  # control: equal passes
    rows["w"] = _row({"a.py:f": 2}, {"h": 1})
    assert ratchet.main(["--baseline", str(baseline)]) == 1
    assert ratchet.main(["--baseline", str(baseline), "--update"]) == 0
    assert ratchet.json.loads(baseline.read_text())["w"]["calls_total"] == 2
    rows["w"] = _row({"a.py:f": 5}, {"h": 1})
    assert ratchet.main(["--baseline", str(baseline), "--update"]) == 1
    assert ratchet.json.loads(baseline.read_text())["w"]["calls_total"] == 2  # not raised
    assert ratchet.main(["--baseline", str(baseline), "--update", "--allow-increase"]) == 0
    assert ratchet.json.loads(baseline.read_text())["w"]["calls_total"] == 5


def test_update_refuses_a_changed_output_unless_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline.json"
    before = _row({"a.py:f": 3}, {"h": 1}, {"n": 1})
    rows = {"w": _row({"a.py:f": 2}, {"h": 1}, {"n": 0})}  # cheaper, but does something else
    monkeypatch.setattr(ratchet, "WORKLOADS", {"w": None})
    monkeypatch.setattr(ratchet, "measure", lambda name: rows[name])
    baseline.write_text(ratchet.json.dumps({"w": before}))
    for flags in (["--update"], ["--update", "--allow-increase"]):
        assert ratchet.main(["--baseline", str(baseline), *flags]) == 1, flags
        assert ratchet.json.loads(baseline.read_text())["w"] == before, flags  # unchanged
    assert ratchet.main(["--baseline", str(baseline), "--update", "--accept-output"]) == 0
    assert ratchet.json.loads(baseline.read_text())["w"]["output"] == {"n": 0}


def test_counter_counts_a_coroutine_once_however_often_it_resumes(tmp_path: Path) -> None:
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "m.py").write_text(
        "import asyncio\n"
        "def leaf():\n    return 1\n"
        "async def waits(n):\n"
        "    for _ in range(n):\n        await asyncio.sleep(0)\n    return leaf()\n"
    )
    sys.path.insert(0, str(src))
    try:
        import m  # type: ignore[import-not-found]

        def count(n: int) -> dict[str, int]:
            counter = ratchet.CallCounter(src)
            sys.setprofile(counter)
            try:
                asyncio.run(m.waits(n))
            finally:
                sys.setprofile(None)
            return dict(counter.counts)

        # 1 suspension or 50: the same count, and the leaf is still seen (positive control).
        assert count(1) == count(50) == {"m.py:waits": 1, "m.py:leaf": 1}
    finally:
        sys.path.remove(str(src))
        sys.modules.pop("m", None)


def test_unstubbed_request_is_recorded_even_when_the_caller_swallows_it() -> None:
    up = upstream.Upstream()
    ok = up.answer(httpx.Request("GET", "https://huggingface.co/api/datasets/a/b"))
    assert ok.status_code == 200 and up.unstubbed == []  # control: a known route answers
    # What an enrichment step's `except Exception` does to the raise:
    with contextlib.suppress(upstream.Unstubbed):
        up.answer(httpx.Request("GET", "https://unknown.example/x"))
    assert up.unstubbed == ["https://unknown.example/x"]
    assert up.requests == {"huggingface.co": 1, "unknown.example": 1}
