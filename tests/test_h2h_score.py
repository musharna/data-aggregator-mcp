"""The head-to-head harness (``benchmarks/h2h/run.py``): its list scorer and run setup.

Moved from ``benchmarks/h2h/check_score_list.py`` so CI runs it. Seen to fail under
``mutate-run`` on: ids matched as substrings (GSE123 inside GSE1234), the
``zenodo.org/records/<n>`` alias dropped, a rejected id counted as pending, the
temporary working directory left in a saved answer, the dam release unpinned, and the
user's settings loaded (PR body has the runs). The fairness controls were seen to fail
on 14 more: an OSF URL form dropped, a version-less id matching a versioned one, the
same-deposit fold dropped, a server arm without Read, ToolUniverse's cache shared, a
finished run re-run, an aborted attempt deleted, a timeout or a budget stop counted
valid, bare required to call tools, the leak scan reading inputs only or not removing
the run's own directory, the oracle's wrong list unchecked, and a version mismatch let
through.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

H2H = Path(__file__).resolve().parents[1] / "benchmarks" / "h2h"
sys.path.insert(0, str(H2H))
import run  # noqa: E402

TASK = {
    "id": "X",
    "kind": "list",
    "studies": [
        {"key": "S1", "ids": ["GSE123", "PRJNA9"]},
        {"key": "S2", "ids": ["10.5281/zenodo.555"]},
        {"key": "S3", "ids": ["PXD000001"]},
        {"key": "S4", "ids": ["zenodo:4895080"]},
    ],
    "optional": [{"key": "B1", "ids": ["KU212370"]}],
}
ADJUDICATION = {
    "X": {
        "add": [{"key": "A1", "ids": ["E-MTAB-7"]}],
        "reject": ["GSE999"],
        "alias": {"S3": ["MSV000123"]},
    }
}


def _run_dir(tmp_path: Path, listed: list | str) -> Path:
    d = tmp_path / "run"
    (d / "out").mkdir(parents=True)
    body = listed if isinstance(listed, str) else json.dumps(listed)
    (d / "out" / "datasets.json").write_text(body)
    return d


def test_score_list_recall_duplicates_precision_and_pending(tmp_path: Path) -> None:
    r = run.score_list(
        TASK,
        _run_dir(
            tmp_path,
            [
                {"id": "GSE123", "archive": "GEO", "same_as": ["PRJNA9"]},  # S1
                {"id": "PRJNA9", "archive": "SRA"},  # S1 again: a duplicate
                {"id": "https://zenodo.org/records/555", "archive": "Zenodo"},  # S2
                {"id": "GSE1234", "archive": "GEO"},  # not S1: whole tokens only
                {"id": "doi:10.1/x", "archive": "Dryad"},  # unknown: pending
                {"id": "GSE999", "archive": "GEO"},  # rejected in adjudication
                {"id": "E-MTAB-7", "archive": "ArrayExpress"},  # added in adjudication
                {"id": "KU212370", "archive": "GenBank"},  # borderline: relevant, no recall
                {"id": "10.5281/zenodo.4895080", "archive": "Zenodo"},  # S4, keyed zenodo:N
                {"id": "MSV000123", "archive": "MassIVE"},  # S3 via an adjudicated alias
            ],
        ),
        ADJUDICATION,
    )
    assert r["json_ok"] and r["listed"] == 10 and r["key_size"] == 5, r
    assert r["found_keys"] == ["A1", "S1", "S2", "S3", "S4"] and r["recall"] == 1.0, r
    assert r["duplicates"] == 1, r
    assert [p["id"] for p in r["pending"]] == ["GSE1234", "doi:10.1/x"], r
    assert r["relevant"] == 7 and r["irrelevant"] == 1 and r["precision"] == 0.875, r


def test_score_list_reads_a_bad_file_as_an_empty_list(tmp_path: Path) -> None:
    bad = run.score_list(TASK, _run_dir(tmp_path, "not json"), ADJUDICATION)
    assert not bad["json_ok"] and bad["recall"] == 0.0 and bad["listed"] == 0, bad


def test_portable_rewrites_the_output_root_and_the_temporary_cwd(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "dam" / "R1" / "r1"
    said = f"I listed 3 studies in `{rd.resolve()}/out/datasets.json`."
    assert run._portable(said, rd) == "I listed 3 studies in `<out>/dam/R1/r1/out/datasets.json`."
    assert run._portable("no path here", rd) == "no path here"  # control: untouched
    # Round 4 runs in a mktemp directory whose out/ is copied to run_dir/out.
    cwd = "/tmp/h2h-abc123"
    said = f"Saved to {cwd}/out/datasets.json"
    assert run._portable(said, rd, cwd) == "Saved to <out>/dam/R1/r1/out/datasets.json"


def _cfg(**kw: object) -> argparse.Namespace:
    base = {
        "dam_dir": None,
        "dam_version": "0.74.0",
        "tu_version": "1.5.7",
        "exclude_newer": "2026-10-17T00:00:00Z",
    }
    return argparse.Namespace(**{**base, **kw})


def test_server_arms_are_pinned_releases_with_a_dependency_cutoff() -> None:
    dam = run.mcp_config("dam", _cfg())["mcpServers"]["data-aggregator"]
    assert dam["command"] == "uvx"
    assert dam["args"] == ["--exclude-newer", "2026-10-17T00:00:00Z", "data-aggregator-mcp==0.74.0"]
    tu = run.mcp_config("tu", _cfg())["mcpServers"]["tooluniverse"]
    assert tu["args"] == ["--exclude-newer", "2026-10-17T00:00:00Z", "tooluniverse==1.5.7"]
    assert run.mcp_config("web", _cfg()) == {"mcpServers": {}}
    assert run.server_spec("dam", _cfg()) == "data-aggregator-mcp==0.74.0"
    # A checkout is still possible, and is recorded as one, never as the release.
    assert run.server_spec("dam", _cfg(dam_dir="/x")) == "checkout:/x"
    assert (
        run.mcp_config("dam", _cfg(dam_dir="/x"))["mcpServers"]["data-aggregator"]["command"]
        == "uv"
    )


def test_every_arm_gets_no_settings_and_the_same_system_prompt() -> None:
    cmds = {
        arm: run.command(arm, "p", Path("mcp.json"), True, model="claude-opus-5-5")
        for arm in ("dam", "tu", "web", "bare")
    }
    for arm, cmd in cmds.items():
        assert cmd[cmd.index("--setting-sources") + 1] == "", arm
        assert cmd[cmd.index("--system-prompt") + 1] == run.SYSTEM_PROMPT, arm
        assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5", arm
        assert "--strict-mcp-config" in cmd, arm
    # The arms differ in their tools and nothing else.
    tools = {arm: cmd[cmd.index("--tools") + 1] for arm, cmd in cmds.items()}
    # Server arms can Read the file an oversized tool result is saved to; bare cannot search.
    assert tools == {
        "dam": "Read,Write",
        "tu": "Read,Write",
        "web": "WebSearch,WebFetch,Bash,Read,Write",
        "bare": "Write",
    }
    one = run.command("dam", "p", Path("mcp.json"), False)
    assert one[one.index("--tools") + 1] == "Read"  # a one-dataset task writes no list
    assert run.mcp_config("bare", _cfg()) == {"mcpServers": {}}
    assert run.MODEL == "claude-opus-5-5"


def test_tooluniverse_gets_its_own_cache_and_home_per_run(tmp_path: Path) -> None:
    env = run.mcp_config("tu", _cfg(), tmp_path)["mcpServers"]["tooluniverse"]["env"]
    assert env["TOOLUNIVERSE_CACHE_DIR"] == str(tmp_path / "cache")
    assert env["TOOLUNIVERSE_CACHE_PATH"] == str(tmp_path / "cache" / "cache.sqlite")
    assert env["TOOLUNIVERSE_HOME"] == str(tmp_path / "home")
    assert env["TOOLUNIVERSE_TMPDIR"] == str(tmp_path / "tmp")
    assert env["PYTHONIOENCODING"] == "utf-8"  # control: the README's own setting stays


def test_build_tasks_reproduces_round3_and_sorts_borderline_studies(tmp_path: Path) -> None:
    import build_tasks

    round3 = H2H / "round3"
    assert build_tasks.build(round3) == json.loads((round3 / "tasks.json").read_text())
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "U9.yaml").write_text(
        "task: U9\nprompt: >-\n  Find every\n  dataset.\nstudies:\n"
        "  - {key: S1, ids: [GSE1], evidence: firm}\n"
        "  - {key: S2, ids: [GSE2], evidence: 'confidence: borderline (x)'}\n"
        "  - {key: S3, ids: [GSE3], confidence: borderline, evidence: y}\n"
    )
    (u9,) = build_tasks.build(tmp_path, frozen="2026-10-16")
    assert u9["prompt"] == "Find every dataset." and u9["frozen"] == "2026-10-16"
    assert [s["key"] for s in u9["studies"]] == ["S1"]  # control: the firm study is scored
    assert [s["key"] for s in u9["optional"]] == ["S2", "S3"]


def _key(*ids: str) -> list[dict]:
    return [{"key": "S1", "ids": list(ids)}]


def _found(listed_id: str, *key_ids: str) -> bool:
    return run.score_entries([{"id": listed_id}], _key(*key_ids), [], [])["recall"] == 1.0


def test_url_forms_name_the_ids_their_archives_key_by() -> None:
    assert _found("https://osf.io/hx2sj/", "10.17605/OSF.IO/HX2SJ")
    assert _found(
        "https://figshare.com/articles/dataset/Bee_videos/21234567", "10.6084/m9.figshare.21234567"
    )
    assert _found("https://data.mendeley.com/datasets/4rd8hwfvsk/2", "10.17632/4rd8hwfvsk")
    assert _found("https://dandiarchive.org/dandiset/000123/draft", "DANDI:000123")
    assert _found("https://openneuro.org/datasets/ds003498/versions/1.0.0", "openneuro:ds003498")
    assert _found("https://zenodo.org/records/555", "10.5281/zenodo.555")
    assert not _found("https://osf.io/hx2sk/", "10.17605/OSF.IO/HX2SJ")  # control: another node


def test_versions_fold_only_inside_one_deposit_and_accessions_match_exactly() -> None:
    # Same deposit: a listed version names the version-less DOI the key holds.
    assert _found("10.6084/m9.figshare.21234567.v3", "10.6084/m9.figshare.21234567")
    assert _found("10.17632/4rd8hwfvsk.1", "10.17632/4rd8hwfvsk")
    # Accessions: a version-less id is not the versioned record, and versions differ.
    assert not _found("GCA_002006685.2", "GCA_002006685")
    assert not _found("GCA_002006685", "GCA_002006685.2")
    assert not _found("GCA_002006685.1", "GCA_002006685.2")
    assert _found("GCA_002006685", "GCA_002006685")  # control: the exact id matches
    assert _found("GCA_002006685.2", "GCA_002006685.2")


def test_oracle_every_round_key_scores_100_and_a_wrong_list_0() -> None:
    for name in ("round2", "round3", "round4", "round5"):
        tasks = json.loads((H2H / name / "tasks.json").read_text())
        assert run.oracle_problems(tasks) == [], name
        o = run.oracle(tasks[0])
        assert o["key"]["recall"] == 1.0 and o["wrong"]["recall"] == 0.0, name


def test_oracle_check_catches_a_scorer_that_cannot_match_or_matches_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = json.loads((H2H / "round3" / "tasks.json").read_text())[:1]
    assert run.oracle_problems(tasks) == []  # control
    monkeypatch.setattr(run, "_mentions", lambda text, alias: False)
    assert any("key scores" in p for p in run.oracle_problems(tasks))
    monkeypatch.setattr(run, "_mentions", lambda text, alias: True)
    assert any("wrong list" in p for p in run.oracle_problems(tasks))


def _session(run_dir: Path, *, tools: list[str], servers: list[dict], calls: list[str],
             result: dict | None, outputs: tuple[str, ...] = (), cwd: str = "/tmp/h2h-x",
             meta: dict | None = None) -> Path:  # fmt: skip
    (run_dir / "out").mkdir(parents=True)
    (run_dir / "out" / "datasets.json").write_text(json.dumps([{"id": "GSE123"}]))
    events = [{"type": "system", "subtype": "init", "cwd": cwd, "tools": tools,
               "mcp_servers": servers, "model": "m", "claude_code_version": "v"}]  # fmt: skip
    for i, name in enumerate(calls):
        events.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": {"q": "x"}}]}})  # fmt: skip
        out = outputs[i] if i < len(outputs) else "ok"
        events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}})  # fmt: skip
    if result is not None:
        events.append({"type": "result", "num_turns": 2, "usage": {}, **result})
    (run_dir / "session.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    base = {"exit": 0, "stderr": "", "wall_s": 1.0, "cwd": cwd}
    (run_dir / "meta.json").write_text(json.dumps({**base, **(meta or {})}))
    return run_dir


OK = {"is_error": False, "subtype": "success", "result": "done", "total_cost_usd": 0.1}
DAM_SERVER = [{"name": "data-aggregator", "status": "connected"}]
DAM_TOOLS = ["Read", "Write", "mcp__data-aggregator__search"]


def test_validity_is_one_rule_for_every_arm(tmp_path: Path) -> None:
    def row(name: str, arm: str = "dam", **kw: object) -> dict:
        args = {"tools": DAM_TOOLS, "servers": DAM_SERVER, "calls": ["mcp__data-aggregator__search"],
                "result": OK, "meta": {"tools": ["Read", "Write"]}} | kw  # fmt: skip
        return run.score(TASK, arm, _session(tmp_path / name, **args))

    good = row("good")
    assert good["valid"] and good["invalid_reason"] == [] and good["recall"] == 0.25, good
    assert row("timeout", meta={"exit": "timeout", "tools": ["Read", "Write"]})[
        "invalid_reason"
    ] == ["exit timeout"]
    capped = row("budget", result={**OK, "is_error": True, "subtype": "error_max_budget_usd"})
    assert capped["invalid_reason"] == ["error error_max_budget_usd"]
    assert row("noresult", result=None)["invalid_reason"] == ["no result"]
    assert row("nocall", calls=["Write"])["invalid_reason"] == ["no data-tool call"]
    assert row("tools", tools=["Write", "mcp__data-aggregator__search"])["invalid_reason"] == [
        "tools ['Write'] not ['Read', 'Write']"
    ]
    down = row("down", servers=[{"name": "data-aggregator", "status": "failed"}])
    assert down["invalid_reason"] == ["servers {'data-aggregator': 'failed'}"]
    # Old rounds' meta.json has no tool list: they are checked against what they ran with.
    old = row("old", tools=["Write", "mcp__data-aggregator__search"], meta={})
    assert old["valid"], old
    # bare has no data tools to call; web's are its built-ins.
    bare = row("bare", "bare", tools=["Write"], servers=[], calls=[], meta={"tools": ["Write"]})
    assert bare["valid"], bare
    web_tools = ["WebSearch", "WebFetch", "Bash", "Read", "Write"]
    web = row("web", "web", tools=web_tools, servers=[], calls=["WebSearch"],
              meta={"tools": web_tools})  # fmt: skip
    assert web["valid"], web
    web_idle = row("webidle", "web", tools=web_tools, servers=[], calls=["Write"],
                   meta={"tools": web_tools})  # fmt: skip
    assert web_idle["invalid_reason"] == ["no data-tool call"]


def test_leak_scan_flags_the_key_or_benchmark_in_tool_traffic_and_not_the_runs_own_dir(
    tmp_path: Path,
) -> None:
    def row(name: str, out: str, cwd: str = "/tmp/h2h-x") -> dict:
        return run.score(TASK, "dam", _session(
            tmp_path / name, tools=DAM_TOOLS, servers=DAM_SERVER,
            calls=["mcp__data-aggregator__search"], result=OK, outputs=(out,), cwd=cwd,
            meta={"tools": ["Read", "Write"]}))  # fmt: skip

    raw = "see https://raw.githubusercontent.com/musharna/data-aggregator-mcp/main/benchmarks/h2h/round4/keys/U2.yaml"
    hit = row("raw", raw)
    assert not hit["valid"] and hit["leaks"] and hit["invalid_reason"][0].startswith("leak"), hit
    assert row("key", "cat benchmarks/h2h/round3/tasks.json")["leaks"]
    assert row("adj", "../../adj_T3.json")["leaks"]
    assert row("r5key", "cat keys/V10.yaml")["leaks"]  # round 5's keys and pool
    assert row("r5pool", "pool_V3.json")["leaks"]
    # An old round ran inside its run directory, whose path names the round: its own
    # Write of datasets.json is no leak.
    own = "/runs/r3_runs/dam/T1/r1"
    assert row("own", f"wrote {own}/datasets.json", cwd=own)["leaks"] == []
    assert row("other", "ls /runs/r3_runs/web/T1/r1", cwd=own)["leaks"]  # another run is
    assert row("clean", "GSE123 found")["valid"]  # control


def _fake_claude(calls: list, outcome: object = 0):
    def fake(cmd, cwd, stdout, **kw):
        calls.append({"cmd": cmd, "cwd": cwd, "env": kw.get("env")})
        cfg = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text())
        calls[-1]["mcp"] = cfg
        if outcome == "timeout":
            raise run.subprocess.TimeoutExpired(cmd, 1)
        stdout.write(json.dumps({"type": "result", **OK}) + "\n")
        return run.subprocess.CompletedProcess(cmd, 0, "", "")

    return fake


def test_a_finished_run_is_never_rerun_and_an_aborted_one_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(model="m", budget_usd="1", claude_code="v")
    task = {**TASK, "prompt": "Find every dataset."}
    calls: list = []
    monkeypatch.setattr(run.subprocess, "run", _fake_claude(calls, "timeout"))
    rd = run.run_one(task, "tu", 1, tmp_path, cfg)
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["exit"] == "timeout" and meta["tools"] == ["Read", "Write"], meta
    state = Path(calls[0]["mcp"]["mcpServers"]["tooluniverse"]["env"]["TOOLUNIVERSE_HOME"]).parent
    assert not state.exists()  # the per-run ToolUniverse state is deleted with the run
    monkeypatch.setattr(run.subprocess, "run", _fake_claude(calls))
    run.run_one(task, "tu", 1, tmp_path, cfg)
    assert len(calls) == 1  # a timeout is a result: not run again
    # A run the harness died in (no meta.json) is moved aside, logged, and run again.
    dead = tmp_path / "dam" / "X" / "r1"
    dead.mkdir(parents=True)
    (dead / "session.jsonl").write_text("partial\n")
    run.run_one(task, "dam", 1, tmp_path, cfg)
    assert len(calls) == 2
    assert (tmp_path / "_aborted" / "dam-X-r1-1" / "session.jsonl").read_text() == "partial\n"
    log = [json.loads(x) for x in (tmp_path / "aborted.jsonl").read_text().splitlines()]
    assert [(e["arm"], e["task"], e["rep"]) for e in log] == [("dam", "X", 1)]
    assert json.loads((dead / "meta.json").read_text())["exit"] == 0  # control: it ran


def test_preflight_stops_on_a_bad_oracle_or_an_unpinned_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = json.loads((H2H / "round4" / "tasks.json").read_text())[:1]
    args = _cfg(arms="dam,tu,web,bare", out=tmp_path)
    seen = {"data-aggregator-mcp": "0.74.0", "tooluniverse": "1.5.7"}
    monkeypatch.setattr(run, "installed_versions", lambda p, v, x: {p: seen[p], "httpx": "0.28.1"})
    run.preflight(tasks, args)  # control: everything as pinned
    assert json.loads((tmp_path / "versions.json").read_text())["dam"]["httpx"] == "0.28.1"
    seen["tooluniverse"] = "1.5.8"
    with pytest.raises(SystemExit, match="tu: uvx resolved tooluniverse 1.5.8, not 1.5.7"):
        run.preflight(tasks, args)
    seen["tooluniverse"] = "1.5.7"
    monkeypatch.setattr(run, "_mentions", lambda text, alias: False)
    with pytest.raises(SystemExit, match="oracle check failed"):
        run.preflight(tasks, args)


def _rl(status: str = "allowed", five: float = 0.3, seven: float = 0.4, resets: float = 0) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": resets, "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": resets},
            "seven_day": {"utilization": seven, "resetsAt": resets + 1}}}}  # fmt: skip


def _claude_script(calls: list, outcomes: list[list[dict]]):
    """A fake ``claude -p`` whose n-th call writes the n-th event list as its transcript."""

    def fake(cmd, cwd, stdout, **kw):
        calls.append(cmd)
        for e in outcomes[min(len(calls), len(outcomes)) - 1]:
            stdout.write(json.dumps(e) + "\n")
        return run.subprocess.CompletedProcess(cmd, 0, "", "")

    return fake


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fresh usage gate, and a fake clock: sleeping advances it, nothing really waits."""
    clock = {"now": 1_000_000.0, "slept": []}
    monkeypatch.setattr(run, "_GATE", {"resume_at": 0.0, "stopped": None})
    monkeypatch.setattr(run.time, "time", lambda: clock["now"])

    def sleep(s: float) -> None:
        clock["slept"].append(s)
        clock["now"] += s

    monkeypatch.setattr(run.time, "sleep", sleep)
    return clock


def test_usage_gate_reruns_a_rate_limited_run_once_and_pauses_until_the_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: dict
) -> None:
    cfg = _cfg(model="m", budget_usd="1", claude_code="v")
    task = {**TASK, "prompt": "Find every dataset."}
    reset = gate["now"] + 600
    rejected = [_rl("rejected", 1.0, 0.5, reset)]  # the limit, before any answer
    answered = [_rl(), {"type": "result", **OK}]
    calls: list = []
    monkeypatch.setattr(run.subprocess, "run", _claude_script(calls, [rejected, answered]))
    rd = run.run_one(task, "dam", 1, tmp_path, cfg)
    assert len(calls) == 2  # run once more, after the pause
    assert sum(gate["slept"]) >= 600  # held until the window reset (+60 s)
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["api_failure"] is None and meta["rate_limit"]["status"] == "allowed"
    kept = tmp_path / "_ratelimited" / "dam-X-r1-1" / "meta.json"
    assert json.loads(kept.read_text())["api_failure"] == "rate_limited"
    log = [json.loads(x) for x in (tmp_path / "ratelimited.jsonl").read_text().splitlines()]
    assert [(e["arm"], e["rep"]) for e in log] == [("dam", 1)]
    pauses = [json.loads(x) for x in (tmp_path / "pauses.jsonl").read_text().splitlines()]
    assert len(pauses) == 1 and pauses[0]["until"] == reset + 60
    # Control: an answered run under the limit is one call, no pause, no retry.
    run.run_one(task, "web", 1, tmp_path, cfg)
    assert len(calls) == 3 and len((tmp_path / "pauses.jsonl").read_text().splitlines()) == 1
    # The rule reads the transcript: a rejection the run then got past is no failure.
    assert run.api_failure(rejected) == "rate_limited"
    assert run.api_failure([*rejected, {"type": "result", **OK}]) is None
    # A second rejection stays as the run's row (invalid): never a third attempt.
    calls.clear()
    monkeypatch.setattr(run.subprocess, "run", _claude_script(calls, [rejected, rejected]))
    rd = run.run_one(task, "tu", 1, tmp_path, cfg)
    assert len(calls) == 2
    assert json.loads((rd / "meta.json").read_text())["api_failure"] == "rate_limited"


def test_usage_gate_pauses_at_five_hour_085_and_stops_at_seven_day_080(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: dict
) -> None:
    cfg = _cfg(model="m", budget_usd="1", claude_code="v")
    task = {**TASK, "prompt": "Find every dataset."}
    calls: list = []
    hot = [_rl("allowed_warning", 0.85, 0.5, gate["now"] + 300), {"type": "result", **OK}]
    monkeypatch.setattr(run.subprocess, "run", _claude_script(calls, [hot]))
    run.run_one(task, "dam", 1, tmp_path, cfg)
    assert gate["slept"] == []  # the run that read 0.85 is a result, not re-run
    run.run_one(task, "dam", 2, tmp_path, cfg)
    assert sum(gate["slept"]) >= 300 and len(calls) == 2  # the next one waited
    weekly = [_rl("allowed", 0.2, 0.80, gate["now"] + 300), {"type": "result", **OK}]
    monkeypatch.setattr(run.subprocess, "run", _claude_script(calls, [weekly, weekly, weekly]))
    calls.clear()
    assert run.run_one(task, "dam", 3, tmp_path, cfg) is not None
    assert run.run_one(task, "web", 3, tmp_path, cfg) is None  # stopped: nothing runs
    assert len(calls) == 1 and run._GATE["stopped"]
    stop = json.loads((tmp_path / "pauses.jsonl").read_text().splitlines()[-1])
    assert stop["stop"] is True and stop["seven_day"] == 0.80


def test_stop_at_lowers_the_seven_day_stop_for_one_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: dict
) -> None:
    cfg = _cfg(model="m", budget_usd="1", claude_code="v")
    task = {**TASK, "prompt": "Find every dataset."}
    run._GATE["stop_at"] = 0.60
    calls: list = []
    below = [_rl("allowed", 0.2, 0.59, gate["now"] + 300), {"type": "result", **OK}]
    at = [_rl("allowed", 0.2, 0.60, gate["now"] + 300), {"type": "result", **OK}]
    monkeypatch.setattr(run.subprocess, "run", _claude_script(calls, [below, at, at]))
    assert run.run_one(task, "dam", 1, tmp_path, cfg) is not None  # 0.59: keeps going
    assert run.run_one(task, "dam", 2, tmp_path, cfg) is not None  # reads 0.60
    assert run.run_one(task, "web", 2, tmp_path, cfg) is None  # stopped below 0.80
    assert len(calls) == 2 and run._GATE["stopped"] == "seven_day 0.6 >= 0.6"
    # The flag reaches the gate, and its default stays 0.80.
    monkeypatch.setattr(run, "preflight", lambda *a: None)
    monkeypatch.setattr(run, "claude_version", lambda: "v")
    monkeypatch.setattr(run, "run_one", lambda *a: None)
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([task]))
    for argv, want in (([], 0.80), (["--stop-at", "0.6"], 0.60)):
        monkeypatch.setattr(sys, "argv", ["run.py", "--tasks", str(tasks), "--out",
                                              str(tmp_path / "o"), *argv])  # fmt: skip
        run.main()
        assert run._GATE["stop_at"] == want


def test_server_spec_runs_dam_from_a_pushed_commit_and_preflight_checks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    spec = f"git+https://github.com/musharna/data-aggregator-mcp@{sha}"
    dam = run.mcp_config("dam", _cfg(server_spec=spec))["mcpServers"]["data-aggregator"]
    assert dam["args"] == ["--exclude-newer", "2026-10-17T00:00:00Z", "--from", spec,
                           "data-aggregator-mcp"]  # fmt: skip
    assert run.server_spec("dam", _cfg(server_spec=spec)) == spec
    assert run.server_spec("dam", _cfg()) == "data-aggregator-mcp==0.74.0"  # control
    tasks = json.loads((H2H / "round4" / "tasks.json").read_text())[:1]
    installed = {"data-aggregator-mcp": "0.75.1", "data-aggregator-mcp@commit": sha}
    monkeypatch.setattr(run, "installed_versions", lambda p, v, x, s=None: dict(installed))
    args = _cfg(arms="dam", out=tmp_path, server_spec=spec)
    run.preflight(tasks, args)  # control: the commit asked for is the one installed
    versions = json.loads((tmp_path / "versions.json").read_text())
    assert versions["dam"]["server_spec"] == spec
    installed["data-aggregator-mcp@commit"] = "f" * 40
    with pytest.raises(SystemExit, match=f"installed commit f{{40}}, not {sha}"):
        run.preflight(tasks, args)
    with pytest.raises(SystemExit, match="is not git"):
        run.preflight(tasks, _cfg(arms="dam", out=tmp_path, server_spec=spec[:-1]))


def test_adjudication_verdicts_merge_into_the_scorers_format_and_rescore(tmp_path: Path) -> None:
    import adjudicate

    pool = {"X": [{"id": "GSE777", "same_as": ["PRJNA777"]}, {"id": "E-MTAB-9"},
                  {"id": "MSV000555"}, {"id": "GSE999"}, {"id": "zenodo:42"}]}  # fmt: skip
    verdicts = {"X": [
        {"id": "GSE777", "verdict": "add:A1"}, {"id": "E-MTAB-9", "verdict": "add:A1"},
        {"id": "MSV000555", "verdict": "alias:S3"}, {"id": "GSE999", "verdict": "reject"},
        {"id": "zenodo:42", "verdict": "optional:B1"}]}  # fmt: skip
    adj, reasons = adjudicate.merge(verdicts, pool)
    assert adj["X"]["add"] == [{"key": "A1", "ids": ["E-MTAB-9", "GSE777", "PRJNA777"]}]
    assert adj["X"]["alias"] == {"S3": ["MSV000555"]} and adj["X"]["reject"] == ["GSE999"]
    assert len(reasons) == 5
    listed = [{"id": "GSE777"}, {"id": "MSV000555"}, {"id": "GSE999"}, {"id": "zenodo:42"}]
    s = run.score_list(TASK, _run_dir(tmp_path, listed), adj)
    assert s["found_keys"] == ["A1", "S3"] and s["irrelevant"] == 1 and not s["pending"], s
    with pytest.raises(SystemExit, match="no verdict for GSE999"):  # every entry is judged
        adjudicate.merge({"X": verdicts["X"][:3]}, pool)
    with pytest.raises(SystemExit, match="bad verdict"):
        adjudicate.merge({"X": [*verdicts["X"][:4], {"id": "zenodo:42", "verdict": "maybe"}]}, pool)


def test_valid_only_comparison_drops_invalid_runs_and_tasks_an_arm_lacks() -> None:
    import analyze

    def row(task: str, arm: str, recall: float, valid: bool = True) -> dict:
        return {"task": task, "arm": arm, "recall": recall, "valid": valid, "relevant": 1,
                "listed": 1, "tool_calls": 1, "cost_usd": 1.0, "wall_s": 1.0}  # fmt: skip

    rows = [
        row("A", "dam", 0.5), row("A", "web", 0.9), row("A", "web", 0.7, valid=False),
        row("B", "dam", 0.6), row("B", "web", 0.2, valid=False),  # web: no valid run
        row("C", "dam", 0.4), row("C", "web", 0.1),
    ]  # fmt: skip
    got = {c["metric"]: c for c in analyze.compare("s", rows) if c["b"] == "web"}
    primary, valid = got["recall"], got["recall_valid_only"]
    # Primary: an invalid run scores 0 (A: web 0.45, B: web 0); dam higher on all 3.
    assert (primary["tasks"], primary["a_higher"], primary["b_higher"]) == (3, 3, 0)
    # Valid-only: A's invalid web run is dropped (web 0.9 > dam), B drops out.
    assert (valid["tasks"], valid["a_higher"], valid["b_higher"]) == (2, 1, 1)
    assert valid["mean_diff"] == pytest.approx(((0.5 - 0.9) + (0.4 - 0.1)) / 2)
