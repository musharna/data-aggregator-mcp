import json
import os
import subprocess
import sys

import pytest

from data_aggregator_mcp import server


def test_search_cli_emits_results_array(monkeypatch, capsys):
    async def fake_dispatch(name, args):
        assert name == "search"
        assert args["query"] == "rice tapetum"
        assert args["size"] == 3
        assert args["sources"] == ["zenodo", "datacite"]
        return {
            "query": "rice tapetum",
            "total": 1,
            "count": 1,
            "results": [
                {
                    "id": "zenodo:1",
                    "source": "zenodo",
                    "kind": "dataset",
                    "title": "Rice tapetum atlas",
                    "doi": "10.5281/zenodo.1",
                    "year": 2023,
                    "creators": [{"name": "A. Lee"}],
                    "description": "single-cell rice",
                }
            ],
            "errors": {},
        }

    monkeypatch.setattr(server, "_dispatch", fake_dispatch)
    server.main(["search", "--json", "--size", "3", "--sources", "zenodo,datacite", "rice tapetum"])
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["title"] == "Rice tapetum atlas"
    assert out[0]["doi"] == "10.5281/zenodo.1"


def test_bare_main_starts_server(monkeypatch):
    recorded = {}

    def fake_run(coro):
        recorded["coro_name"] = getattr(coro, "__qualname__", "") or coro.cr_code.co_name
        coro.close()  # we are not actually awaiting it

    monkeypatch.setattr(server.asyncio, "run", fake_run)
    server.main([])
    assert "_serve" in recorded["coro_name"]


def _page(results, errors):
    return {
        "query": "q",
        "total": len(results),
        "count": len(results),
        "results": results,
        "errors": errors,
    }


def test_search_cli_exits_nonzero_when_every_source_failed(monkeypatch, capsys):
    # The router reports a failing source in ``errors`` rather than raising, so
    # the OTHER sources can still answer. The CLI used to drop that dict and
    # print ``[]`` with exit 0 - an outage read as "no hits". Positive control
    # in the same test: a partial failure still emits the surviving results
    # with exit 0, and names the failed source on stderr.
    outage = {
        "zenodo": "UpstreamUnavailableError: Zenodo search exhausted 3 retries (last HTTP 504)"
    }
    hit = {"id": "datacite:1", "source": "datacite", "kind": "dataset", "title": "t"}

    async def all_failed(name, args):
        return _page([], outage)

    monkeypatch.setattr(server, "_dispatch", all_failed)
    with pytest.raises(SystemExit) as exc:
        server.main(["search", "--json", "--sources", "zenodo", "single cell"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "zenodo: UpstreamUnavailableError" in captured.err
    assert "every source failed" in captured.err

    async def partial(name, args):
        return _page([hit], outage)

    monkeypatch.setattr(server, "_dispatch", partial)
    server.main(["search", "--json", "--sources", "zenodo,datacite", "single cell"])
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [hit]
    assert "zenodo: UpstreamUnavailableError" in captured.err
    assert "every source failed" not in captured.err


def _zenodo_retry_budget_s() -> float:
    """Worst case the CLI can legitimately take before it exits on its own.

    Derived from the constants the code retries with, so the test's timeout
    cannot drift below the code's budget again. The previous fixed 90 s was
    LESS than 3 x 30 s + backoff, so a Zenodo 504 storm surfaced here as
    ``TimeoutExpired`` (a failure) instead of the non-zero exit (a skip) the
    test was written to handle - and blocked merges on an unrelated repo.
    """
    from data_aggregator_mcp import _http, zenodo

    backoff = sum(min(1.0 * 2**i, _http._RETRY_AFTER_CAP) for i in range(zenodo.MAX_RETRIES - 1))
    return zenodo.DEFAULT_TIMEOUT * zenodo.MAX_RETRIES + backoff + 30.0  # +interpreter/import slack


@pytest.mark.skipif(os.environ.get("RECAP_NO_NET") == "1", reason="offline")
def test_search_cli_real_subprocess():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "data_aggregator_mcp",
            "search",
            "--json",
            "--size",
            "2",
            "--sources",
            "zenodo",
            "single cell",
        ],
        capture_output=True,
        text=True,
        timeout=_zenodo_retry_budget_s(),
    )
    if proc.returncode != 0:
        pytest.skip(f"da search unavailable: {proc.stderr[:200]}")
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    # Non-vacuous: exit 0 now means at least one source answered, and "single
    # cell" on Zenodo has thousands of hits, so an empty list is a defect.
    assert len(data) == 2, proc.stderr[:200]
    assert all("title" in r for r in data)
