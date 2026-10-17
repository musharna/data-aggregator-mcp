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
    for name in ("round2", "round3", "round4"):
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
