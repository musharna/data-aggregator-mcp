"""Head-to-head: the same agent finds and downloads datasets through one MCP server.

Each run is a headless ``claude -p`` session with exactly one arm's tools: ``dam``
(data-aggregator-mcp), ``tu`` (ToolUniverse), ``web`` (no MCP: web search, web fetch
and a shell) or ``bare`` (no way to search at all: the from-memory floor). The harness, not the agent, scores the run from the session's stream-json
log and the files it left on disk (see ``score``).

Isolation (round 4 on): every run starts in a fresh ``mktemp`` directory under /tmp
(nothing above it holds a CLAUDE.md or project settings), with ``--setting-sources ""``
(no user, project or local settings, so no hooks or plugins) and the same short
``--system-prompt`` in every arm. Both server arms are pinned PyPI releases started
with ``uvx --exclude-newer``, so their dependencies resolve as of one fixed date.

    python3 benchmarks/h2h/run.py --tasks round4/tasks.json --out <runs> --workers 1
    python3 benchmarks/h2h/run.py --score-only --tasks round4/tasks.json --out <runs>

``--server-spec git+https://github.com/musharna/data-aggregator-mcp@<40-hex sha>`` runs
the dam arm on an unreleased commit instead of the PyPI pin (``uvx --from``, with the same
``--exclude-newer`` for its dependencies); preflight checks the installed package came
from that commit, and the spec is recorded in every row and in ``versions.json``.

``--model`` (default ``MODEL``) is the model every arm runs on. A run set holds one model:
preflight refuses an ``--out`` started on another, ``versions.json`` records the model asked
for and the models served, and a run served another model than it asked for is invalid.

Usage gate (added 2026-10-18; the runs share the account's limits with its owner): after
every run its transcript's last ``rate_limit_event`` is read and kept in ``meta.json``.
A five-hour utilization at or above ``PAUSE_AT`` or a ``rejected`` status holds every
later run until that window's ``resetsAt`` (each pause appended to ``pauses.jsonl``); a
seven-day utilization at or above ``--stop-at`` (default ``STOP_AT``) stops the sweep. A run the rate limit
rejected before the model answered (``api_failure``) is a harness failure, not a result:
it is moved to ``_ratelimited/``, logged in ``ratelimited.jsonl`` and run once more after
the pause; a second such failure stays as the run's (invalid) row.

Standard library only, so it runs without this package's environment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Rounds 1-3 ran claude-sonnet-5-5 with a $3 cap, ToolUniverse 1.5.4 (rounds 1, 2) and
# 1.5.6 (round 3), and the dam arm from a checkout; their score files are history.
MODEL = "claude-opus-5-5"
DAM_VERSION = "0.74.0"  # latest on PyPI, 2026-10-16
TU_VERSION = "1.5.7"  # latest on PyPI, 2026-10-16
# Dependencies of both servers resolve as of this instant (after both releases).
EXCLUDE_NEWER = "2026-10-17T00:00:00Z"
RUN_TIMEOUT_S = 15 * 60
PAUSE_AT = 0.85  # five-hour utilization that holds the sweep until the window resets
STOP_AT = 0.80  # seven-day utilization that stops the sweep
SERVER_SPEC = re.compile(r"git\+https://github\.com/musharna/data-aggregator-mcp@([0-9a-f]{40})")
BUDGET_USD = "6"  # Opus 5.5 costs twice Sonnet 5.5 per token
SYSTEM_PROMPT = (
    "You are a research assistant who finds public scientific datasets. Use only the "
    "tools you have. Work on your own until the task is done: nobody will answer questions."
)

PROMPT_TAIL = (
    "\n\nSave the file into this directory: {out}\n"
    "When you are done, reply with the identifier (accession, DOI or record id) of the "
    "dataset you found and the path of the file you saved."
)

# Round 2: "find every dataset" tasks are scored from a list the agent writes.
LIST_TAIL = (
    "\n\nWrite your answer to this file: {out}/datasets.json\n"
    "It must be a JSON list with one object per distinct study: "
    '{{"id": "<main identifier>", "archive": "<archive>", "title": "<title>", '
    '"same_as": ["<identifiers of the same study in other archives>"]}}. '
    "List each study once, with its copies in other archives under same_as.\n"
    "When you are done, reply with the number of studies you listed."
)


def server_spec(arm: str, cfg: argparse.Namespace) -> str | None:
    """What the arm's server is, as recorded in every score row."""
    if arm == "dam":
        if getattr(cfg, "server_spec", None):
            return cfg.server_spec
        return (
            f"checkout:{cfg.dam_dir}" if cfg.dam_dir else f"data-aggregator-mcp=={cfg.dam_version}"
        )
    if arm == "tu":
        return f"tooluniverse=={cfg.tu_version}"
    return None


# Built-in tools per arm, for list tasks (one-dataset tasks drop Write). Every arm that
# searches gets Read: Claude Code saves a tool result too large to return to a file and
# hands the agent its path, and without Read that page is lost (ToolUniverse hit it in 24
# of 30 round-3 runs; rounds 1 to 3 gave the server arms Write only). ``bare`` has no
# data tools at all: what the model lists from memory, a floor for the other arms.
ARM_TOOLS = {
    "dam": ("Read", "Write"),
    "tu": ("Read", "Write"),
    "web": ("WebSearch", "WebFetch", "Bash", "Read", "Write"),
    "bare": ("Write",),
}
ARM_SERVER = {"dam": "data-aggregator", "tu": "tooluniverse"}
# The calls that count as using the arm's data tools; bare has none to use.
DATA_TOOLS = {"web": frozenset({"WebSearch", "WebFetch", "Bash"})}


# What rounds 1 to 3 and the round-4 pilot gave each arm (their meta.json does not say;
# every init event of theirs matches this). The validity rule checks old runs against it.
ARM_TOOLS_BEFORE_READ = {
    "dam": ("Write",),
    "tu": ("Write",),
    "web": ("WebSearch", "WebFetch", "Bash", "Read", "Write"),
}


def arm_tools(arm: str, writes_list: bool) -> list[str]:
    return [t for t in ARM_TOOLS[arm] if writes_list or t != "Write"]


def mcp_config(arm: str, cfg: argparse.Namespace, state: Path | None = None) -> dict:
    pin = ["--exclude-newer", cfg.exclude_newer]
    if arm == "dam":
        if cfg.dam_dir:  # a checkout under development, not a release: say so in rows
            args = ["run", "--no-sync", "--directory", cfg.dam_dir, "data-aggregator-mcp"]
            return {"mcpServers": {"data-aggregator": {"command": "uv", "args": args}}}
        if getattr(cfg, "server_spec", None):  # an unreleased commit, pushed to GitHub
            args = [*pin, "--from", cfg.server_spec, "data-aggregator-mcp"]
            return {"mcpServers": {"data-aggregator": {"command": "uvx", "args": args}}}
        # The README's `uvx data-aggregator-mcp`, pinned to the release under test.
        server = {"command": "uvx", "args": [*pin, f"data-aggregator-mcp=={cfg.dam_version}"]}
        return {"mcpServers": {"data-aggregator": server}}
    if arm == "tu":
        # ToolUniverse's README configuration, pinned to the release under test.
        env = {"PYTHONIOENCODING": "utf-8"}
        if state is not None:
            # ToolUniverse keeps a persistent result cache (~/.tooluniverse/cache.sqlite)
            # and a workspace across processes: a later run would read an earlier run's
            # answers. Each run gets its own, deleted with it. CACHE_PATH outranks
            # CACHE_DIR in ToolUniverse 1.5.7, so an inherited one must be overridden too.
            env |= {
                "TOOLUNIVERSE_HOME": str(state / "home"),
                "TOOLUNIVERSE_CACHE_DIR": str(state / "cache"),
                "TOOLUNIVERSE_CACHE_PATH": str(state / "cache" / "cache.sqlite"),
                "TOOLUNIVERSE_TMPDIR": str(state / "tmp"),
            }
        server = {"command": "uvx", "args": [*pin, f"tooluniverse=={cfg.tu_version}"], "env": env}
        return {"mcpServers": {"tooluniverse": server}}
    return {"mcpServers": {}}


def command(
    arm: str,
    prompt: str,
    config: Path,
    writes_list: bool = False,
    *,
    model: str = MODEL,
    budget: str = BUDGET_USD,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        # No user, project or local settings: no hooks, plugins or permission rules.
        "--setting-sources",
        "",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--max-budget-usd",
        budget,
        "--permission-mode",
        "bypassPermissions",
    ]
    # Server arms get no built-in tool that searches or downloads: the server is their
    # only way to the archives.
    cmd += ["--tools", ",".join(arm_tools(arm, writes_list))]
    return cmd


def claude_version() -> str:
    proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _events(log: Path) -> list[dict]:
    out = []
    for line in log.read_text().splitlines() if log.exists() else []:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def rate_limit(events: list[dict]) -> dict | None:
    """The run's last ``rate_limit_event``, reduced to what the usage gate reads."""
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
        "five_hour_resets": (w.get("five_hour") or {}).get("resetsAt") or info.get("resetsAt"),
        "seven_day": (w.get("seven_day") or {}).get("utilization"),
        "seven_day_resets": (w.get("seven_day") or {}).get("resetsAt"),
    }


def api_failure(events: list[dict]) -> str | None:
    """``rate_limited`` when the account's limit rejected the run before the model
    answered: a ``rejected`` rate-limit event and no successful result. Read from the
    transcript alone, never from the list or its score, so one rule picks rows in every
    arm. A timeout or a budget stop is not one: it stays a result."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    ok = result is not None and result.get("subtype") == "success" and not result.get("is_error")
    rejected = any(
        e.get("type") == "rate_limit_event"
        and (e.get("rate_limit_info") or {}).get("status") == "rejected"
        for e in events
    )
    return "rate_limited" if rejected and not ok else None


# The usage gate, shared by every worker: no run starts before _GATE["resume_at"], and
# none at all once the seven-day window passed STOP_AT.
_GATE: dict = {"resume_at": 0.0, "stopped": None}
_GATE_LOCK = threading.Lock()


def _log(root: Path, name: str, entry: dict) -> None:
    with _GATE_LOCK, (root / name).open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _hold(root: Path) -> bool:
    """Wait out a pause; False once the sweep is stopped."""
    while True:
        with _GATE_LOCK:
            stopped, resume_at = _GATE["stopped"], _GATE["resume_at"]
        if stopped:
            return False
        wait = resume_at - time.time()
        if wait <= 0:
            why = spend_stop()
            if why:
                with _GATE_LOCK:
                    _GATE["stopped"] = f"spend gate: {why}"
                _log(root, "pauses.jsonl", {"at": time.time(), "stop": True, "spend_gate": why})
                return False
            return True
        time.sleep(min(wait, 60))


def spend_stop() -> str | None:
    """Why an API-billed sweep must stop before its next run, or None.

    Set ``BENCH_SPEND_GATE`` to the shared spend meter's script and each run
    first asks it (``check``, track from ``BENCH_TRACK``); any non-zero exit
    (spend cap, billing violation, checkpoint) stops the sweep cleanly.
    """
    gate = os.environ.get("BENCH_SPEND_GATE")
    if not gate:
        return None
    p = subprocess.run([sys.executable, gate, "check"], capture_output=True, text=True, check=False)
    return None if p.returncode == 0 else (p.stderr.strip() or f"spend gate exit {p.returncode}")


def probe_seven_day() -> float | None:
    """A fresh seven-day reading: the newest ``rate_limit_event`` of a one-word Haiku run
    with no tools, settings or servers. None if the probe fails or reports no window.

    Added 2026-10-21: the server sometimes reports a spurious second weekly window
    (0.98, resetting on another day) beside the real one (0.69 by /usage), so a reading
    at the stop line is checked by a fresh probe before it stops the sweep."""
    d = Path(tempfile.mkdtemp(prefix="h2h-probe-", dir="/tmp"))  # nosec B108 - as _attempt
    try:
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        cmd = ["claude", "-p", "say ok", "--model", "claude-haiku-5-5", "--output-format",
               "stream-json", "--verbose", "--no-session-persistence", "--setting-sources", "",
               "--strict-mcp-config", "--mcp-config", str(d / "mcp.json"), "--tools", ""]  # fmt: skip
        proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})  # fmt: skip
        events = []
        for line in proc.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return (rate_limit(events) or {}).get("seven_day")
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(d)


def _observe(root: Path, run_dir: Path, rl: dict | None) -> None:
    """Pause or stop the sweep on the reading a run just left. A seven-day reading at or
    above the stop line stops the sweep only if a fresh probe agrees (or gives no
    reading); otherwise the reading is logged and the sweep goes on."""
    if rl is None:
        return
    now = time.time()
    stop_at = _GATE.get("stop_at", STOP_AT)
    if (rl.get("seven_day") or 0) >= stop_at:
        fresh = probe_seven_day()
        if fresh is None or fresh >= stop_at:
            with _GATE_LOCK:
                _GATE["stopped"] = f"seven_day {rl['seven_day']} >= {stop_at} (probe {fresh})"
            _log(root, "pauses.jsonl", {"at": now, "stop": True, "probe": fresh,
                                        "after": str(run_dir), **rl})  # fmt: skip
            return
        _log(root, "pauses.jsonl", {"at": now, "spurious_seven_day": True, "probe": fresh,
                                    "after": str(run_dir), **rl})  # fmt: skip
    if rl.get("status") == "rejected" or (rl.get("five_hour") or 0) >= PAUSE_AT:
        until = float(rl.get("five_hour_resets") or now + 3600) + 60
        with _GATE_LOCK:
            _GATE["resume_at"] = max(_GATE["resume_at"], until)
        _log(root, "pauses.jsonl", {"at": now, "until": until, "after": str(run_dir), **rl})


def run_one(task: dict, arm: str, rep: int, root: Path, cfg: argparse.Namespace) -> Path | None:
    """One (task, arm, rep) through the usage gate: None if the sweep was stopped first."""
    run_dir = root / arm / task["id"] / f"r{rep}"
    if (run_dir / "meta.json").exists():
        # Finished, whatever the outcome: a timeout, a budget stop or an error is a
        # result (scored invalid), never silently re-run.
        return run_dir
    for attempt in (1, 2):
        if not _hold(root):
            return None
        _attempt(task, arm, rep, root, cfg)
        meta = json.loads((run_dir / "meta.json").read_text())
        _observe(root, run_dir, meta.get("rate_limit"))
        if meta.get("api_failure") != "rate_limited" or attempt == 2:
            return run_dir
        # Rejected by the account's limit before any answer: a harness failure. Kept,
        # logged, and run once more after the pause.
        dest = root / "_ratelimited" / f"{arm}-{task['id']}-r{rep}-1"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_dir), dest)
        _log(root, "ratelimited.jsonl", {"arm": arm, "task": task["id"], "rep": rep,
                                         "kept": str(dest)})  # fmt: skip
    return run_dir


def _attempt(task: dict, arm: str, rep: int, root: Path, cfg: argparse.Namespace) -> Path:
    run_dir = root / arm / task["id"] / f"r{rep}"
    if run_dir.exists():
        # The harness itself died mid-run (no meta.json). The same rule for every arm:
        # keep the attempt under _aborted/, log it, and run again.
        n = len(list((root / "_aborted").glob(f"{arm}-{task['id']}-r{rep}-*")))
        dest = root / "_aborted" / f"{arm}-{task['id']}-r{rep}-{n + 1}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_dir), dest)
        with (root / "aborted.jsonl").open("a") as fh:
            fh.write(
                json.dumps({"arm": arm, "task": task["id"], "rep": rep, "kept": str(dest)}) + "\n"
            )
    run_dir.mkdir(parents=True)
    log = run_dir / "session.jsonl"
    # A fresh, empty working directory outside any project: no CLAUDE.md above it, no
    # earlier run's files. The answer is copied back into run_dir afterwards. /tmp, not
    # $TMPDIR: a session's TMPDIR can sit under ~/.claude, which holds settings.
    # mkdtemp makes a fresh 0700 directory with a random name, so nothing can pre-place it.
    cwd = Path(tempfile.mkdtemp(prefix="h2h-", dir="/tmp"))  # nosec B108 - see above
    state = Path(tempfile.mkdtemp(prefix="h2h-state-", dir="/tmp"))  # nosec B108 - as cwd
    try:
        out = cwd / "out"
        out.mkdir()
        config = run_dir / "mcp.json"
        config.write_text(json.dumps(mcp_config(arm, cfg, state)))
        tail = LIST_TAIL if task.get("kind") == "list" else PROMPT_TAIL
        prompt = task["prompt"] + tail.format(out=out)
        started = time.monotonic()
        with log.open("w") as fh:
            try:
                proc = subprocess.run(
                    command(
                        arm,
                        prompt,
                        config,
                        task.get("kind") == "list",
                        model=cfg.model,
                        budget=cfg.budget_usd,
                    ),
                    cwd=cwd,
                    stdout=fh,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=RUN_TIMEOUT_S,
                    # One run must not leave notes a later run reads.
                    env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                )
                status = proc.returncode
                err = proc.stderr[-4000:]
            except subprocess.TimeoutExpired:
                status, err = "timeout", ""
        shutil.copytree(out, run_dir / "out")
    finally:  # whatever happened, no run's files or ToolUniverse state outlive it
        shutil.rmtree(cwd)
        shutil.rmtree(state)
    meta = {
        "exit": status,
        "stderr": err,
        "wall_s": time.monotonic() - started,
        "cwd": str(cwd),
        "model_requested": cfg.model,
        "claude_code": cfg.claude_code,
        "server": server_spec(arm, cfg),
        "tools": arm_tools(arm, task.get("kind") == "list"),
    }
    events = _events(log)
    meta["rate_limit"] = rate_limit(events)
    meta["api_failure"] = api_failure(events)
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return run_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _norm(s: str) -> str:
    s = s.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if s.startswith(prefix):
            return s[len(prefix) :]
    return s


# The URL forms an agent writes for a record whose id is a DOI or a prefixed id the URL
# does not contain, each mapped to the ids it names. A URL that already contains the id
# (doi.pangaea.de/10.1594/..., ncbi.nlm.nih.gov/geo/...?acc=GSE1) needs no rule: ids
# match as whole tokens anywhere in the entry.
_URL_FORMS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(pattern), templates)
    for pattern, templates in (
        (r"zenodo\.org/(?:records?|deposit|uploads)/(\d+)", ("10.5281/zenodo.{0}",)),
        (
            r"figshare\.com/articles/(?:[a-z_]+/)?(?:[^/\s]+/)?(\d{5,})",
            ("10.6084/m9.figshare.{0}", "figshare:{0}"),
        ),
        (r"osf\.io/([a-z0-9]{5})(?![a-z0-9])", ("10.17605/osf.io/{0}", "osf:{0}")),
        (r"data\.mendeley\.com/datasets/([a-z0-9]{10})(?![a-z0-9])", ("10.17632/{0}",)),
        (r"dandiarchive\.org/dandiset/(\d{6})", ("dandi:{0}",)),
        (r"10\.48324/dandi\.(\d{6})", ("dandi:{0}",)),
        (r"openneuro\.org/datasets/(ds\d{6})", ("openneuro:{0}",)),
        (r"cellxgene\.cziscience\.com/(?:collections|e)/([0-9a-f-]{36})", ("cellxgene:{0}",)),
        (r"humancellatlas\.org/explore/projects/([0-9a-f-]{36})", ("hca:{0}",)),
        (r"crcns\.org/data-sets/[a-z0-9]+/([a-z0-9-]+)", ("crcns:{0}",)),
    )
)
# DOI registries whose versions are versions of ONE deposit: a listed version also names
# the version-less DOI. Accessions are not folded: GCA_002006685.2 is a different
# assembly from GCA_002006685.1, and a key may hold them as different studies.
_SAME_DEPOSIT_VERSION = re.compile(
    r"(?:(10\.6084/m9\.figshare\.\d+)\.v\d+"
    r"|(10\.17632/[a-z0-9]{10})\.\d+"
    r"|(10\.18112/openneuro\.ds\d{6})\.v\d+(?:\.\d+)*)(?![0-9a-z])"
)
_HEX_ID = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f])"
)


def _canonical(text: str) -> str:
    """``text`` (an entry's ids, normalized) plus the ids its URL forms and versioned
    same-deposit DOIs name, so ``https://osf.io/hx2sj/`` matches ``10.17605/OSF.IO/HX2SJ``."""
    extra = [t.format(*m.groups()) for rx, ts in _URL_FORMS for m in rx.finditer(text) for t in ts]
    extra += [next(g for g in m.groups() if g) for m in _SAME_DEPOSIT_VERSION.finditer(text)]
    return " ".join([text, *extra])


def _aliases(ids: list[str]) -> list[str]:
    """A study's ids, plus the other ways an agent writes them: a Zenodo record's DOI,
    ``zenodo:N`` and URL forms; the version-less DOI of a same-deposit version; the local
    part of a prefixed id when it is distinctive (``openneuro:ds003498`` -> ``ds003498``);
    a UUID or 32-hex id inside a longer one (``GNPS task <hash>``)."""
    out = [_norm(i) for i in ids]
    for i in list(out):
        m = re.fullmatch(r"(?:10\.5281/zenodo\.|zenodo:)(\d+)", i)
        if m:
            n = m.group(1)
            out += [
                f"10.5281/zenodo.{n}",
                f"zenodo:{n}",
                f"zenodo.org/records/{n}",
                f"zenodo.org/record/{n}",
            ]
        out += [g for m in _SAME_DEPOSIT_VERSION.finditer(i) for g in m.groups() if g]
        prefix, sep, local = i.partition(":")
        distinctive = len(local) >= 6 and any(c.isdigit() for c in local)
        if sep and "/" not in prefix and not prefix.startswith("http") and distinctive:
            out.append(local)
        out += _HEX_ID.findall(i)
    return sorted(set(out))


def _mentions(text: str, alias: str) -> bool:
    """Whole token only, so GSE12 does not match GSE123. A version-less id does not match
    a versioned one either (``GCA_002006685`` is not ``GCA_002006685.2``); same-deposit
    DOI versions reach their version-less form through ``_canonical`` instead."""
    rx = rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])(?!\.v?\d)"
    return re.search(rx, text) is not None


def score_list(task: dict, run_dir: Path, adjudication: dict) -> dict:
    """Recall, duplicates and precision of the agent's datasets.json against the key.

    The key is the pre-built studies plus any adjudicated additions (pooling); an entry
    that matches neither the key nor a rejected id is ``pending`` adjudication.
    """
    adj = adjudication.get(task["id"], {})
    # ``alias``: an id adjudication found to be another copy of a study already keyed.
    extra = adj.get("alias", {})
    studies = [
        {**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
        for s in [*task["studies"], *adj.get("add", [])]
    ]
    # Borderline studies: listing one is not wrong, missing one is not a miss.
    optional = [
        {**s, "ids": [*s["ids"], *extra.get(s["key"], [])]}
        for s in [*task.get("optional", []), *adj.get("optional", [])]
    ]
    rejected = [_norm(i) for i in adj.get("reject", [])]
    try:
        listed = json.loads((run_dir / "out" / "datasets.json").read_text())
        json_ok = isinstance(listed, list) and all(isinstance(e, dict) for e in listed)
    except (OSError, ValueError):
        listed, json_ok = [], False
    if not json_ok:
        listed = []
    return score_entries(listed, studies, optional, rejected, json_ok)


def score_entries(
    listed: list[dict],
    studies: list[dict],
    optional: list[dict],
    rejected: list[str],
    json_ok: bool = True,
) -> dict:
    """``score_list`` on entries already read: what the oracle and the baselines use."""
    hits: dict[str, int] = {}
    relevant, irrelevant, pending = 0, 0, []
    for entry in listed:
        ids = [entry.get("id"), *(entry.get("same_as") or [])]
        text = _canonical(" ".join(_norm(str(i)) for i in ids if i))
        matched = [s["key"] for s in studies if any(_mentions(text, a) for a in _aliases(s["ids"]))]
        for key in matched:
            hits[key] = hits.get(key, 0) + 1
        if matched or any(any(_mentions(text, a) for a in _aliases(s["ids"])) for s in optional):
            relevant += 1
        elif any(_mentions(text, r) for r in rejected):
            irrelevant += 1
        else:
            pending.append({"id": entry.get("id"), "same_as": entry.get("same_as") or [],
                            "title": entry.get("title")})  # fmt: skip
    archives = sorted({str(e.get("archive", "")).strip() for e in listed} - {""})
    return {
        "json_ok": json_ok,
        "listed": len(listed),
        "key_size": len(studies),
        "found_keys": sorted(hits),
        "recall": round(len(hits) / len(studies), 3) if studies else None,
        # A study listed as two or more separate entries.
        "duplicates": sum(1 for n in hits.values() if n > 1),
        "relevant": relevant,
        "irrelevant": irrelevant,
        "pending": pending,
        "precision": round(relevant / (relevant + irrelevant), 3)
        if relevant + irrelevant
        else None,
        "archives_listed": archives,
    }


def oracle(task: dict) -> dict[str, dict]:
    """The scorer's positive and negative controls for one task, with no agent.

    ``key``: every keyed study listed once, by all its ids -> recall 1.0, precision 1.0,
    no duplicates, nothing pending. ``wrong``: ids no study in the key has (each id with
    a suffix that makes it another record) -> recall 0.0.
    """
    every = [*task["studies"], *task.get("optional", [])]
    key = [{"id": s["ids"][0], "same_as": s["ids"][1:]} for s in every]
    wrong = [{"id": f"{s['ids'][0]}9x"} for s in every]
    return {
        "key": score_entries(key, task["studies"], task.get("optional", []), []),
        "wrong": score_entries(wrong, task["studies"], task.get("optional", []), []),
    }


def oracle_problems(tasks: list[dict]) -> list[str]:
    """Every task whose key the scorer cannot score as 100% (or a wrong list above 0%)."""
    out = []
    for t in tasks:
        if t.get("kind") != "list":
            continue
        o = oracle(t)
        k, w = o["key"], o["wrong"]
        if not (k["recall"] == 1.0 and k["precision"] == 1.0 and k["duplicates"] == 0
                and not k["pending"]):  # fmt: skip
            out.append(
                f"{t['id']}: key scores {k['recall']}/{k['precision']}, dup {k['duplicates']}"
            )
        if w["recall"] != 0.0 or w["relevant"]:
            out.append(f"{t['id']}: a wrong list scores recall {w['recall']}")
    return out


def _result_sources(events: list[dict]) -> list[str]:
    """Distinct ``source`` values in this server's tool results: did the fan-out fire?"""
    found: set[str] = set()
    for e in events:
        if e.get("type") != "user":
            continue
        for block in e.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = json.dumps(content) if not isinstance(content, str) else content
            found.update(re.findall(r'\\?"source\\?":\s*\\?"([a-z_]+)', text))
    return sorted(found)


# What no transcript may touch: an answer key or adjudication file, this benchmark in the
# repo (a checkout, or GitHub: the repo is public, so the keys are on the web), and the
# run directories and score files of any round. Rounds 1 to 3 kept their runs and key
# drafts in a session job directory (.claude/jobs/...); a run of theirs ran inside it.
_LEAK = re.compile(
    r"benchmarks/h2h|answer_key|adjudication(?:_reasons)?\.(?:json|yaml)"
    r"|round[1-5]/(?:keys|tasks\.json|scores)|keys/[rtuv]\d+\.yaml"
    r"|data-aggregator-mcp-artifacts|\.claude/jobs/"
    r"|(?<![0-9a-z])(?:h2h_runs|r[2-5]_(?:runs|rerun|pilot|\d+|\d+_3arm)|r3_071)(?![0-9a-z])"
    r"|(?<![0-9a-z])(?:adj|pool)_[rtuv]\d+\.json"
    r"|github\.com/musharna/data-aggregator-mcp/(?:tree|blob|raw)/[^/\s\"]+/benchmarks"
    r"|raw\.githubusercontent\.com/musharna/data-aggregator-mcp/[^/\s\"]+/benchmarks",
    re.IGNORECASE,
)


def _tool_traffic(events: list[dict]) -> list[tuple[str, str]]:
    """Every tool input and tool result in the log, as ("input" | "output", text)."""
    out = []
    for e in events:
        for block in (
            e.get("message", {}).get("content", []) if isinstance(e.get("message"), dict) else []
        ):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                out.append(("input", json.dumps(block.get("input"))))
            elif block.get("type") == "tool_result":
                c = block.get("content")
                out.append(("output", c if isinstance(c, str) else json.dumps(c)))
    return out


# A path inside an installed tool (uv's cache, which holds the dam arm's git checkout,
# a site-packages, pipx or node_modules tree), and what in such a path is still a leak:
# an answer file (the checkout of this repository holds the committed keys).
_INSTALL_PATH = re.compile(
    r"[^\s\"']*(?:/\.cache/uv/|/site-packages/|/\.local/share/uv/|/\.local/pipx/"
    r"|/node_modules/)[^\s\"']*"
)
_ANSWER_FILE = re.compile(
    r"answer_key|adjudication(?:_reasons)?\.(?:json|yaml)"
    r"|round[1-5]/(?:keys|tasks\.json|scores)|keys/[rtuv]\d+\.yaml"
    r"|(?<![0-9a-z])(?:adj|pool)_[rtuv]\d+\.json",
    re.IGNORECASE,
)


def leak_hits(events: list[dict], own: list[str], narrowed: bool = True) -> list[dict]:
    """Where the run touched the key, the benchmark or another run. ``own``: the run's
    own directories, removed first (an old round ran inside its run directory, whose path
    names the round, and every Write of its answer names it).

    ``narrowed`` (the primary rule from 2026-10-21): a tool-install path is not a leak
    unless it names an answer file; ``narrowed=False`` is the rule as first written."""
    hits = []
    for where, text in _tool_traffic(events):
        for path in sorted(filter(None, own), key=len, reverse=True):
            text = text.replace(path, "<own>")
        if narrowed:
            text = _INSTALL_PATH.sub(
                lambda m: m.group(0) if _ANSWER_FILE.search(m.group(0)) else "<install>", text
            )
        hits += [{"where": where, "match": m.group(0)} for m in _LEAK.finditer(text)]
    return hits


def validity(arm: str, writes_list: bool, meta: dict, init: dict, result: dict,
             calls: list[str], leaks: list[dict]) -> list[str]:  # fmt: skip
    """Why the row does not measure the arm, by one rule for every arm (empty: valid).

    The run must finish (no timeout, harness error or budget stop), start with exactly
    its arm's tools and server, use them at least once unless the arm has none (bare),
    and touch nothing of the benchmark (``leak_hits``). A run that says which model it
    asked for (round 4 on) must have been served that model (the init event's)."""
    why = []
    asked = meta.get("model_requested")
    if asked and init.get("model") != asked:
        why.append(f"model {init.get('model')} not {asked}")
    if meta.get("exit") != 0:
        why.append(f"exit {meta.get('exit')}")
    if not result:
        why.append("no result")
    elif result.get("is_error"):
        why.append(f"error {result.get('subtype')}")
    builtin = sorted(t for t in init.get("tools", []) if not t.startswith("mcp__"))
    before = [t for t in ARM_TOOLS_BEFORE_READ.get(arm, ()) if writes_list or t != "Write"]
    expected = meta.get("tools", before)
    if builtin != sorted(expected):
        why.append(f"tools {builtin} not {sorted(expected)}")
    server = ARM_SERVER.get(arm)
    servers = {x.get("name"): x.get("status") for x in init.get("mcp_servers", [])}
    if servers != ({server: "connected"} if server else {}):
        why.append(f"servers {servers}")
    if arm != "bare":
        data = DATA_TOOLS.get(arm) or frozenset()
        used = [c for c in calls if c in data or (server and c.startswith(f"mcp__{server}__"))]
        if not used:
            why.append("no data-tool call")
    if leaks:
        why.append(f"leak {leaks[0]['match']}")
    return why


def score(task: dict, arm: str, run_dir: Path, adjudication: dict | None = None) -> dict:
    """What the run achieved, read from its log and its directory, not its own claims."""
    events = []
    for line in (run_dir / "session.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    init = next((e for e in events if e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    calls = [
        block["name"]
        for e in events
        if e.get("type") == "assistant"
        for block in e.get("message", {}).get("content", [])
        if block.get("type") == "tool_use"
    ]
    usage = result.get("usage") or {}
    answer = (result.get("result") or "").casefold()
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    meta = json.loads((run_dir / "meta.json").read_text())
    own = [init.get("cwd"), meta.get("cwd"), str(run_dir.resolve())]
    leaks = leak_hits(events, own)
    invalid = validity(arm, task.get("kind") == "list", meta, init, result, calls, leaks)
    # Rounds 1-3 recorded none of these in meta.json; their init event still names the
    # model and the Claude Code version.
    provenance = {
        "model": init.get("model") or meta.get("model_requested"),
        "claude_code": init.get("claude_code_version") or meta.get("claude_code"),
        "server": meta.get("server"),
    }
    if task.get("kind") == "list":
        outcome = score_list(task, run_dir, adjudication or {})
        outcome["result_sources"] = _result_sources(events)
    else:
        # The file's sha256, plus any accepted alternative (e.g. the same entry gzipped).
        want = {task["file"]["sha256"], *task["file"].get("also_sha256", [])}
        files = [p for p in run_dir.rglob("*") if p.is_file() and p.name not in
                 ("session.jsonl", "meta.json", "mcp.json")]  # fmt: skip
        got = [str(p.relative_to(run_dir)) for p in files if _sha256(p) in want]
        outcome = {
            "found": any(i.casefold() in answer for i in task["answer_ids"]),
            "bytes_ok": bool(got),
            "files_matching": got,
        }
    return {
        "task": task["id"],
        "arm": arm,
        "run": run_dir.name,
        **provenance,
        "servers": servers,
        "valid": not invalid,
        "invalid_reason": invalid,
        "leaks": leaks,
        # The leak rule as first written (install paths count): the secondary validity.
        "leaks_as_written": leak_hits(events, own, narrowed=False),
        **outcome,
        "tool_calls": len(calls),
        "tools": calls,
        "turns": result.get("num_turns"),
        "input_tokens": usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": result.get("total_cost_usd"),
        "wall_s": round(meta["wall_s"], 1),
        "exit": meta["exit"],
        "error": result.get("subtype") if result.get("is_error") else None,
        # Rounds 1 to 3 ran in the run directory itself; only their init event names it.
        "answer": _portable(
            result.get("result") or "", run_dir, meta.get("cwd") or init.get("cwd")
        )[:400],
    }


def _portable(answer: str, run_dir: Path, cwd: str | None = None) -> str:
    """``answer`` with the output root (``<out>/<arm>/<task>/r<n>``) written as ``<out>``:
    agents name the file they wrote, and that absolute path is this machine's, not the
    result's. A run in a temporary directory (``cwd``) names that directory instead, and
    its ``out/`` was copied to ``run_dir/out``, so it is written as the run directory."""
    root = run_dir.parents[2]
    if cwd:
        answer = answer.replace(cwd, "<out>/" + str(run_dir.relative_to(root)))
    for form in dict.fromkeys((str(root.resolve()), str(root))):
        answer = answer.replace(form, "<out>")
    return answer


def installed_versions(
    package: str, version: str, exclude_newer: str, spec: str | None = None
) -> dict[str, str]:
    """Every distribution in the environment ``uvx`` builds for the arm's server, read
    from that environment, not from what was asked for. With ``spec`` (a git URL), the
    package's ``direct_url.json`` commit is returned too, under ``<package>@commit``."""
    code = (
        "import importlib.metadata as m, json; "
        "v = {d.metadata['Name'].lower(): d.version for d in m.distributions()}; "
        f"u = m.distribution({package!r}).read_text('direct_url.json'); "
        f"v[{package + '@commit'!r}] = (json.loads(u).get('vcs_info') or {{}}).get('commit_id') if u else None; "
        "print(json.dumps(v))"
    )
    proc = subprocess.run(
        ["uvx", "--exclude-newer", exclude_newer, "--from", spec or f"{package}=={version}",
         "python", "-c", code],
        capture_output=True, text=True, check=True, timeout=900,
    )  # fmt: skip
    return json.loads(proc.stdout.strip().splitlines()[-1])


def preflight(tasks: list[dict], args: argparse.Namespace) -> None:
    """Before any run: the scorer scores the key 100% and a wrong list 0%, and each pinned
    server resolves to the version every row will claim. Writes ``versions.json``."""
    problems = oracle_problems(tasks)
    if problems:
        raise SystemExit("oracle check failed: " + "; ".join(problems))
    # One model per run set: a set started on one model is never topped up with another
    # (the Opus round-5 runs stay as they are; a Haiku sweep gets its own --out).
    model = getattr(args, "model", MODEL)
    old = args.out / "versions.json"
    if old.exists():
        before = (json.loads(old.read_text()).get("_model") or {}).get("requested", MODEL)
        if before != model:
            raise SystemExit(f"{args.out} holds {before} runs, not {model}: use another --out")
    versions: dict[str, dict] = {"_model": {"requested": model}}
    arms = args.arms.split(",")
    pins = {
        "dam": ("data-aggregator-mcp", args.dam_version),
        "tu": ("tooluniverse", args.tu_version),
    }
    spec = getattr(args, "server_spec", None)
    for arm, (package, version) in pins.items():
        if arm not in arms or (arm == "dam" and args.dam_dir):
            continue
        if arm == "dam" and spec:
            m = SERVER_SPEC.fullmatch(spec)
            if not m:
                raise SystemExit(f"--server-spec {spec!r} is not git+https://github.com/"
                                 "musharna/data-aggregator-mcp@<40-hex sha>")  # fmt: skip
            got = installed_versions(package, version, args.exclude_newer, spec)
            if got.get(f"{package}@commit") != m.group(1):
                raise SystemExit(f"dam: uvx installed commit {got.get(f'{package}@commit')}, "
                                 f"not {m.group(1)}")  # fmt: skip
            versions[arm] = {**got, "server_spec": spec}
            continue
        got = installed_versions(package, version, args.exclude_newer)
        if got.get(package) != version:
            raise SystemExit(f"{arm}: uvx resolved {package} {got.get(package)}, not {version}")
        versions[arm] = got
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "versions.json").write_text(json.dumps(versions, indent=1, sort_keys=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--arms", default="dam,tu,web,bare")
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", "--parallel", type=int, default=1, dest="workers")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--budget-usd", default=BUDGET_USD)
    ap.add_argument("--dam-version", default=DAM_VERSION)
    ap.add_argument("--tu-version", default=TU_VERSION)
    ap.add_argument("--exclude-newer", default=EXCLUDE_NEWER)
    ap.add_argument("--dam-dir", help="run the dam arm from a checkout, not the release")
    ap.add_argument(
        "--server-spec",
        help="run the dam arm on an unreleased commit: "
        "git+https://github.com/musharna/data-aggregator-mcp@<40-hex sha>",
    )
    ap.add_argument(
        "--stop-at", type=float, default=STOP_AT,
        help=f"seven-day utilization that stops the sweep (default {STOP_AT})",
    )  # fmt: skip
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--adjudication", type=Path, help="list tasks: pooled additions/rejects")
    ap.add_argument(
        "--write-to", type=Path, help="where scores.json and pool.json go (default: --out)"
    )
    args = ap.parse_args()
    tasks = json.loads(args.tasks.read_text())
    adjudication = json.loads(args.adjudication.read_text()) if args.adjudication else {}
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    arms = args.arms.split(",")
    jobs = [(t, a, r) for r in range(1, args.reps + 1) for t in tasks for a in arms]
    _GATE["stop_at"] = args.stop_at
    if not args.score_only:
        preflight(tasks, args)
        args.claude_code = claude_version()
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            futures = [pool.submit(run_one, t, a, r, args.out, args) for t, a, r in jobs]
            for f in concurrent.futures.as_completed(futures):
                if f.result() is not None:
                    print("done", f.result(), flush=True)
        if _GATE["stopped"]:
            print("STOPPED by the usage gate:", _GATE["stopped"], flush=True)
    rows = []
    for t, a, r in jobs:
        run_dir = args.out / a / t["id"] / f"r{r}"
        if (run_dir / "meta.json").exists():
            rows.append(score(t, a, run_dir, adjudication))
    if not args.score_only and rows:
        # The model each run was actually served (its init event), beside the one asked for.
        vpath = args.out / "versions.json"
        versions = json.loads(vpath.read_text())
        versions["_model"]["served"] = sorted({str(r["model"]) for r in rows})
        vpath.write_text(json.dumps(versions, indent=1, sort_keys=True))
    dest = args.write_to or args.out
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scores.json").write_text(json.dumps(rows, indent=1))
    # Pooling: every listed entry no key study or reject covers, with no arm or run
    # attached and in a shuffled order, so adjudication cannot favour an arm.
    pool: dict[str, dict[str, dict]] = {}
    for row in rows:
        for entry in row.get("pending", []):
            pool.setdefault(row["task"], {}).setdefault(_norm(str(entry["id"])), entry)
    shuffled = {}
    for task_id, entries in sorted(pool.items()):
        items = list(entries.values())
        random.Random(task_id).shuffle(items)
        shuffled[task_id] = items
    (dest / "pool.json").write_text(json.dumps(shuffled, indent=1))
    for row in rows:
        if "recall" in row:
            print(
                f"{row['task']} {row['arm']:4} {row['run']} exit={row['exit']} "
                f"valid={row['valid']!s:5} json={row['json_ok']!s:5} recall={row['recall']} listed={row['listed']:3} "
                f"dup={row['duplicates']} prec={row['precision']} pending={len(row['pending'])} "
                f"src={len(row['result_sources'])} calls={row['tool_calls']:3} "
                f"in={row['input_tokens']:>8} ${row['cost_usd'] or 0:.2f} {row['wall_s']}s "
                f"{row['error'] or ''} {'; '.join(row['invalid_reason'])}"
            )
            continue
        print(
            f"{row['task']} {row['arm']:4} {row['run']} valid={row['valid']!s:5} "
            f"found={row['found']!s:5} bytes={row['bytes_ok']!s:5} calls={row['tool_calls']:3} "
            f"in={row['input_tokens']:>8} out={row['output_tokens']:>6} "
            f"${row['cost_usd'] or 0:.2f} {row['wall_s']}s {row['error'] or ''} "
            f"{'; '.join(row['invalid_reason'])}"
        )


if __name__ == "__main__":
    main()
