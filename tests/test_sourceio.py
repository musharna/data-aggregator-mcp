"""How ``sourceio`` reads a source: Range edges, sizes, and error mapping.

The redirect refusals themselves are in ``test_operate_redirects.py``; this file pins the
read mechanics that replaced fsspec and httpfs, against real listeners on 127.0.0.1
(conftest permits private egress here, so the guard passes every hop).
"""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import pytest

pytest.importorskip("fsspec")
pytest.importorskip("pyarrow")

from data_aggregator_mcp import egress, sourceio, tabular
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError, ValidationError
from tests._listeners import listener, serve, status

BODY = b"0123456789"


def test_reads_are_ranges_and_end_cleanly_at_eof():
    with listener() as host, sourceio.open_source(host.url("/f")) as f:
        host.routes["/f"] = serve(BODY)
        assert f.read(3) == b"012"
        assert f.seek(2, io.SEEK_CUR) == 5
        assert f.read(2) == b"56"
        assert f.seek(-2, io.SEEK_END) == 8
        assert f.read(5) == b"89"  # clipped at the end
        assert f.read(5) == b""  # past the end: the server's 416 is an empty read
        assert f.read(0) == b""
        with pytest.raises(ValueError, match=r"^invalid whence 7$"):
            f.seek(0, 7)
        with pytest.raises(ValueError, match=r"^negative seek position -1$"):
            f.seek(-11, io.SEEK_END)
        gets = [rng for method, _, rng in host.hits if method == "GET"]
        assert gets == ["bytes=0-2", "bytes=5-6", "bytes=8-12", "bytes=10-14"]


async def test_the_remote_sniff_asks_for_one_byte_past_the_window():
    n = tabular._CSV_SNIFF_BYTES
    with listener() as host:
        host.routes["/big.csv"] = serve(b"id\n" + b"1\n" * (3 * n))
        out = await tabular.preview(host.url("/big.csv"), "big.csv", n=5)
        assert out["rows"] == [{"id": "1"}] * 5
        assert host.hits == [("GET", "/big.csv", f"bytes=0-{n}")]


def test_a_server_that_ignores_range_serves_a_head_but_not_an_offset():
    with listener() as host:
        host.routes["/t.csv"] = serve(b"a\n" + b"1\n" * 100, ranges=False)
        host.routes["/f"] = serve(BODY, ranges=False)
        # From the start, a whole-file answer still yields the head (positive control)...
        out = asyncio.run(tabular.preview(host.url("/t.csv"), "t.csv", n=2))
        assert out["rows"] == [{"a": "1"}, {"a": "1"}]
        # ...but a read at an offset (a Parquet footer) cannot be served without the
        # whole file.
        url = host.url("/f")
        with sourceio.open_source(url) as f:
            assert f.read(2) == b"01"
            f.seek(5)
            with pytest.raises(
                UpstreamUnavailableError,
                match=rf"^\[UpstreamUnavailableError\] operate source: the server ignores "
                rf"range requests, so {re.escape(url)} can only be read whole; use fetch instead$",
            ):
                f.read(2)


def test_size_falls_back_to_get_and_reports_an_undeclared_size_as_none():
    with listener() as host:
        host.routes["/head-refused"] = serve(BODY, head=False)
        host.routes["/no-length"] = serve(BODY, length=False)
        assert sourceio.size(host.url("/head-refused")) == len(BODY)
        assert [m for m, _, _ in host.hits] == ["HEAD", "GET"]
        assert sourceio.size(host.url("/no-length")) is None
        # Sequential reads need no size; a read from the end does.
        url = host.url("/no-length")
        with sourceio.open_source(url) as f:
            assert f.read(4) == b"0123"
            with pytest.raises(
                UpstreamUnavailableError,
                match=rf"^\[UpstreamUnavailableError\] operate source: the server declares no "
                rf"size for {re.escape(url)}, and reading this file from its end needs one; "
                r"use fetch instead$",
            ):
                f.seek(-1, io.SEEK_END)


def test_http_and_transport_failures_are_named_errors():
    with listener() as host:
        host.routes["/boom"] = lambda h: status(h, 500)
        host.routes["/hang-up"] = lambda h: None  # closes the connection, answers nothing
        host.routes["/ok"] = serve(BODY)
        missing, boom, hang_up = host.url("/missing"), host.url("/boom"), host.url("/hang-up")
        with pytest.raises(
            NotFoundError,
            match=rf"^\[NotFoundError\] operate source → HTTP 404 \({re.escape(missing)}\)$",
        ):
            sourceio.size(missing)
        with (
            pytest.raises(
                UpstreamUnavailableError,
                match=rf"^\[UpstreamUnavailableError\] operate source → HTTP 500 "
                rf"\({re.escape(boom)}\)$",
            ),
            sourceio.local_copy(boom),
        ):
            pass
        with (
            pytest.raises(
                UpstreamUnavailableError,
                match=r"^\[UpstreamUnavailableError\] operate source transport failure: "
                rf"RemoteProtocolError\(.+\) \({re.escape(hang_up)}\)$",
            ),
            sourceio.open_source(hang_up) as f,
        ):
            f.read(1)
        # Positive control: the same calls succeed against a served file.
        assert sourceio.size(host.url("/ok")) == len(BODY)
        with sourceio.local_copy(host.url("/ok")) as local:
            assert Path(local).read_bytes() == BODY
        assert not Path(local).exists()  # the download is removed on exit


def test_local_sources_are_read_in_place(tmp_path):
    f = tmp_path / "a.csv"
    f.write_bytes(BODY)
    assert sourceio.size(f.as_uri()) == len(BODY)
    with sourceio.open_source(f.as_uri()) as fh:
        assert fh.read() == BODY
    with sourceio.local_copy(f.as_uri()) as local:
        assert local == f.as_uri()


async def test_the_sync_hook_refuses_rather_than_skips_on_a_running_loop(monkeypatch):
    class _Req:
        url = "http://127.0.0.1:9/x"

    monkeypatch.delenv(egress.ALLOW_PRIVATE_ENV, raising=False)
    egress._clear_cache()
    # In a worker thread, as sourceio calls it: the check runs and refuses.
    with pytest.raises(ValidationError, match=r"^\[ValidationError\] request: '127\.0\.0\.1' "):
        await asyncio.to_thread(egress.enforce_on_request_sync, _Req())
    _Req.url = "http://8.8.8.8/x"
    await asyncio.to_thread(egress.enforce_on_request_sync, _Req())  # positive control
    # On a thread that runs a loop it raises instead of returning unchecked.
    with pytest.raises(
        RuntimeError,
        match=r"^enforce_on_request_sync called on a running event loop; hook an async client$",
    ):
        egress.enforce_on_request_sync(_Req())
