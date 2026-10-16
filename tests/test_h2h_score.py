"""The head-to-head harness (``benchmarks/h2h/run.py``): its list scorer and run setup.

Moved from ``benchmarks/h2h/check_score_list.py`` so CI runs it. Seen to fail under
``mutate-run`` on: ids matched as substrings (GSE123 inside GSE1234), the
``zenodo.org/records/<n>`` alias dropped, a rejected id counted as pending, the
temporary working directory left in a saved answer, the dam release unpinned, and the
user's settings loaded (PR body has the runs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
        for arm in ("dam", "tu", "web")
    }
    for arm, cmd in cmds.items():
        assert cmd[cmd.index("--setting-sources") + 1] == "", arm
        assert cmd[cmd.index("--system-prompt") + 1] == run.SYSTEM_PROMPT, arm
        assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5", arm
        assert "--strict-mcp-config" in cmd, arm
    # The arms differ in their tools and nothing else.
    tools = {arm: cmd[cmd.index("--tools") + 1] for arm, cmd in cmds.items()}
    assert tools == {"dam": "Write", "tu": "Write", "web": "WebSearch,WebFetch,Bash,Read,Write"}
    assert run.MODEL == "claude-opus-5-5"


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
