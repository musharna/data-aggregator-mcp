"""An upstream that did not answer is told apart from one that answered wrongly.

``UpstreamUnavailableError.down`` is what the nightly live suite reads (via
``tests/_upstream_down.py``) to report an outage as a skip and keep a changed API a
failure, so both sides are checked here against real sockets, and the plugin is
checked by running a pytest session through it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from data_aggregator_mcp import _http
from data_aggregator_mcp import fetch as fetch_mod
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, FileEntry
from tests._listeners import listener, serve, status

pytest_plugins = ["pytester"]

_ROOT = Path(__file__).resolve().parent.parent


def _routes(srv) -> None:
    srv.routes["/ok"] = serve(b'{"a": 1}')
    srv.routes["/hangup"] = lambda h: None  # closes the connection without a response
    srv.routes["/busy"] = lambda h: status(h, 503)
    srv.routes["/gone"] = lambda h: status(h, 410)
    srv.routes["/forbidden"] = lambda h: status(h, 403)
    srv.routes["/garbled"] = serve(b"<html>not json</html>")


async def _raised(coro) -> UpstreamUnavailableError:
    with pytest.raises(UpstreamUnavailableError) as info:
        await coro
    return info.value


async def test_a_host_that_does_not_answer_is_down_and_one_answering_wrongly_is_not() -> None:
    def ask(url: str):
        return _http.request_json(client, "GET", url, service="t", expect=dict, max_retries=1)

    with listener() as srv:
        _routes(srv)
        async with httpx.AsyncClient() as client:
            assert await ask(srv.url("/ok")) == {"a": 1}  # positive control
            hangup = await _raised(ask(srv.url("/hangup")))
            busy = await _raised(ask(srv.url("/busy")))
            gone = await _raised(ask(srv.url("/gone")))
            garbled = await _raised(ask(srv.url("/garbled")))
    assert (hangup.down, busy.down) == (True, True)
    assert (gone.down, garbled.down) == (False, False)
    assert str(busy).startswith("[UpstreamUnavailableError] t exhausted 1 retries")


async def test_a_download_whose_host_does_not_answer_is_down(tmp_path: Path) -> None:
    def one(url: str) -> DataResource:
        entry = FileEntry(name=url.rsplit("/", 1)[1] + ".bin", size=8, url=url)
        return DataResource(
            id="zenodo:1", source="zenodo", kind="dataset", title="t", files=[entry]
        )

    def get(url: str):
        return fetch_mod.fetch_files(client, one(url), dest=str(tmp_path))

    with listener() as srv:
        _routes(srv)
        srv.routes["/file"] = serve(b"12345678")
        async with httpx.AsyncClient() as client:
            assert (await get(srv.url("/file"))).bytes == 8  # positive control
            hangup = await _raised(get(srv.url("/hangup")))
            busy = await _raised(get(srv.url("/busy")))
            forbidden = await _raised(get(srv.url("/forbidden")))
    assert (hangup.down, busy.down, forbidden.down) == (True, True, False)


_SESSION = """
import httpx
import pytest

from data_aggregator_mcp.errors import RateLimitError, UpstreamUnavailableError

live = pytest.mark.skipif(False, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _swallowed(exc):
    try:
        raise exc
    except Exception:
        return None


@pytest.fixture
def down_in_setup():
    raise UpstreamUnavailableError("setup host unreachable", down=True)


@live
def test_down_raised():
    raise UpstreamUnavailableError("raised host unreachable", down=True)


@live
def test_down_caught_then_empty():
    assert _swallowed(UpstreamUnavailableError("caught host unreachable", down=True))


@live
def test_rate_limited_caught_then_empty():
    assert _swallowed(RateLimitError("busy host 429"))


@live
def test_raw_transport():
    raise httpx.ConnectTimeout("raw host")


@live
def test_down_in_a_fixture(down_in_setup):
    pass


@live
def test_answered_wrongly():
    raise UpstreamUnavailableError("changed host → HTTP 410")


@live
def test_plain_failure():
    assert 1 == 2


@live
def test_passes_though_a_host_was_down():
    _swallowed(UpstreamUnavailableError("other host unreachable", down=True))


def test_not_live_down():
    raise UpstreamUnavailableError("mocked host unreachable", down=True)
"""


def test_a_live_test_failed_by_a_down_host_is_skipped_and_every_other_failure_stays(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    down_file = pytester.path / "down.json"
    monkeypatch.setenv("PYTHONPATH", str(_ROOT))
    monkeypatch.setenv("DATA_AGGREGATOR_MCP_DOWN_FILE", str(down_file))
    pytester.makepyfile(test_inner=_SESSION)
    result = pytester.runpytest_subprocess(
        "-p", "tests._upstream_down", "-p", "no:randomly", "-p", "no:cacheprovider", "-rfs"
    )
    assert result.parseoutcomes() == {"failed": 3, "skipped": 5, "passed": 1}
    result.stdout.fnmatch_lines(["FAILED*test_answered_wrongly*", "FAILED*test_plain_failure*"])
    result.stdout.fnmatch_lines(["FAILED*test_not_live_down*"])
    down = json.loads(down_file.read_text())
    assert down == {
        "test_inner.py::test_down_raised": (
            "upstream down: [UpstreamUnavailableError] raised host unreachable"
        ),
        "test_inner.py::test_down_caught_then_empty": (
            "upstream down: [UpstreamUnavailableError] caught host unreachable"
        ),
        "test_inner.py::test_rate_limited_caught_then_empty": (
            "upstream down: [RateLimitError] busy host 429"
        ),
        "test_inner.py::test_raw_transport": "upstream down: ConnectTimeout('raw host')",
        "test_inner.py::test_down_in_a_fixture": (
            "upstream down: [UpstreamUnavailableError] setup host unreachable"
        ),
    }


def test_this_suite_runs_through_the_plugin(request: pytest.FixtureRequest) -> None:
    """The plugin is registered by conftest importing it; without that import the
    nightly would report every outage as a failure again, and nothing else would say so."""
    from tests import _upstream_down

    hooks = request.config.pluginmanager.hook.pytest_runtest_makereport.get_hookimpls()
    assert _upstream_down.pytest_runtest_makereport in [h.function for h in hooks]
    assert "_record_down_upstreams" in request.fixturenames
    assert "_isolate_app_env" in request.fixturenames  # positive control: conftest's own
