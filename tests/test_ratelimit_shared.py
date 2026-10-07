"""NCBI requests are spaced machine-wide, not per process.

NCBI throttles the IP. Each server process paced itself at 3/s, so three of them (three
Claude sessions, or the benchmark's parallel runs) sent 9/s: every page of three
concurrent five-search runs lost a stream to HTTP 429, and "axolotl limb regeneration"
fell from 1,652 hits to 0 (2026-10-07). The schedule is now a lock file every process
claims its slot from.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import time
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import _ratelimit
from data_aggregator_mcp._ratelimit import SharedSchedule
from tests.test_ratelimit import FakeClock

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_RATE = 100.0  # 600 slots fill 6 s, inside the 30 s queue horizon
_CLAIMS = 200


def _slots(path: str, n: int, start) -> list[float]:
    """In a fresh process: claim ``n`` slots as fast as possible and return the time each
    was granted for. The slot is read from the claim itself (the clock reading it was
    taken at plus the wait it returned), not from when a sleep happened to end: stamping
    the wake-up failed CI with a 0.066 s gap between 0.1 s slots, one late wake-up on a
    busy runner (2026-10-07)."""
    readings: list[float] = []

    def now() -> float:
        readings.append(time.time())
        return readings[-1]

    schedule = SharedSchedule(Path(path), _RATE, now=now)
    start.wait()  # all three claim at once, so a missing lock has races to lose
    out = []
    for _ in range(n):
        wait = schedule._claim()
        out.append(readings[-1] + wait)
    return out


def test_three_processes_share_one_schedule(tmp_path) -> None:
    path = str(tmp_path / "ncbi.schedule")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager, ctx.Pool(3) as pool:
        start = manager.Barrier(3)
        runs = pool.starmap(_slots, [(path, _CLAIMS, start)] * 3)
    slots = sorted(t for run in runs for t in run)
    assert len(slots) == 3 * _CLAIMS  # positive control: every process got all its slots
    gaps = [b - a for a, b in zip(slots, slots[1:], strict=False)]
    # No two slots closer than one period. Per-process files grant each process its own
    # sequence (gaps ~0); without the lock two claims read the same free slot (gap 0). A
    # gap may be longer: a stall that lets the queue fall behind the clock starts the next
    # slot at "now", 1 run in 12 on a loaded machine. Exact spacing is
    # test_slots_are_spaced_with_no_burst_after_idle's. (1e-5 s: float spacing at today's
    # epoch is ~2e-7.)
    assert min(gaps) >= 1 / _RATE - 1e-5, sorted(gaps)[:5]


async def test_slots_are_spaced_with_no_burst_after_idle(tmp_path) -> None:
    clk = FakeClock()
    clk.t = 1000.0
    waits: list[float] = []

    async def sleep(dt: float) -> None:
        waits.append(round(dt, 6))

    schedule = SharedSchedule(tmp_path / "s", 4.0, now=clk.now, sleep=sleep)
    for _ in range(3):
        await schedule.acquire()
    assert waits == [0.25, 0.5]  # the first slot is free; the next two queue
    # Idle for a minute: a token bucket would now grant a burst. This grants one slot
    # at once, then spaces the rest.
    clk.t += 60.0
    waits.clear()
    for _ in range(3):
        await schedule.acquire()
    assert waits == [0.25, 0.5]


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b"", "first use"),
        (b"not a number", "a file that is not ours"),
        (repr(1000.0 + 3600).encode(), "a slot an hour ahead: a clock that jumped back"),
        (b"nan", "nan compares false both ways"),
    ],
)
async def test_an_unusable_slot_is_replaced_by_now(tmp_path, content, why) -> None:
    clk = FakeClock()
    clk.t = 1000.0
    waits: list[float] = []

    async def sleep(dt: float) -> None:
        waits.append(dt)

    path = tmp_path / "s"
    path.write_bytes(content)
    await SharedSchedule(path, 4.0, now=clk.now, sleep=sleep).acquire()
    assert waits == [], why
    assert float(path.read_bytes()) == pytest.approx(1000.25), why
    # Positive control: a slot within the queue horizon is honoured.
    path.write_bytes(repr(1000.0 + 5).encode())
    await SharedSchedule(path, 4.0, now=clk.now, sleep=sleep).acquire()
    assert waits == [pytest.approx(5.0)], why


async def test_an_unlockable_schedule_paces_this_process_and_says_why(tmp_path, caplog) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    clk = FakeClock()
    schedule = SharedSchedule(blocker / "ncbi.schedule", 2.0, now=clk.now, sleep=clk.sleep)
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp._ratelimit"):
        await schedule.acquire()
        await schedule.acquire()
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1, warnings  # said once, not per request
    assert warnings[0].startswith("NCBI requests are paced in this process only: cannot lock ")
    assert str(blocker / "ncbi.schedule") in warnings[0]
    # It still paces, with no burst: one slot free, the next half a second on.
    assert schedule._local is not None
    assert (schedule._local.rate, schedule._local.capacity) == (2.0, 1.0)
    await schedule._local.acquire()
    assert clk.t == pytest.approx(1.0)


def test_the_state_directory_is_configurable_and_defaults_to_fetchs(monkeypatch) -> None:
    from data_aggregator_mcp import fetch

    monkeypatch.setenv(_ratelimit.STATE_DIR_ENV, "/srv/state")
    assert _ratelimit.state_dir() == Path("/srv/state")
    monkeypatch.delenv(_ratelimit.STATE_DIR_ENV)
    assert _ratelimit.state_dir() == fetch.DEFAULT_CACHE_DIR  # one directory per user


def _esearch_statuses(state: str, n: int) -> list[int]:
    """In a fresh process: ``n`` keyless esearch calls, paced only by the schedule."""
    os.environ[_ratelimit.STATE_DIR_ENV] = state
    os.environ.pop("NCBI_API_KEY", None)

    async def run() -> list[int]:
        out = []
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(n):
                url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                await _ratelimit.acquire("NCBI esearch (gds)", url)
                resp = await client.get(url, params={"db": "gds", "term": f"tardigrade {i}"})
                out.append(resp.status_code)
        return out

    return asyncio.run(run())


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
def test_live_three_processes_stay_under_ncbis_keyless_limit(tmp_path) -> None:
    with multiprocessing.get_context("spawn").Pool(3) as pool:
        runs = pool.starmap(_esearch_statuses, [(str(tmp_path), 6)] * 3)
    statuses = [s for run in runs for s in run]
    assert len(statuses) == 18
    assert statuses.count(429) == 0, statuses
    assert set(statuses) == {200}, statuses
