"""What ``sourceio`` sends and what its errors name, pinned exactly.

``test_sourceio`` covers the read mechanics; this file pins the request the guarded client
sends (headers, timeout, scheme gate) and the URL each failure names, against real
listeners on 127.0.0.1 (conftest permits private egress here).
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

pytest.importorskip("fsspec")

from data_aggregator_mcp import sourceio
from data_aggregator_mcp.errors import UpstreamUnavailableError, ValidationError
from tests._listeners import listener, serve, status

BODY = b"0123456789"


def test_a_malformed_source_url_is_refused_naming_operate():
    url = "http://[::1/f"
    with pytest.raises(
        ValidationError,
        match=r"^\[ValidationError\] operate source: malformed URL \(Invalid IPv6 URL\); "
        r"refusing to fetch http://\[::1/f$",
    ):
        sourceio.size(url)
    with listener() as host:  # positive control: a well-formed URL is read
        host.routes["/f"] = serve(BODY)
        assert sourceio.size(host.url("/f")) == len(BODY)


def test_https_goes_to_the_client_and_a_failure_names_the_url():
    with listener() as host:
        host.routes["/f"] = serve(BODY)
        assert sourceio.size(host.url("/f")) == len(BODY)  # positive control: http
        # https is remote too: the plain-HTTP listener fails the TLS handshake, a transport
        # failure naming the URL, not the scheme refusal a local-only reading would raise.
        url = host.url("/f").replace("http://", "https://")
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] operate source transport failure: "
            rf"ConnectError\(.+\) \({re.escape(url)}\)$",
        ):
            sourceio.size(url)


def test_every_request_asks_for_the_stored_bytes():
    seen: list[tuple[str, str | None]] = []
    inner = serve(BODY)

    def route(h: BaseHTTPRequestHandler) -> None:
        seen.append((h.command, h.headers.get("Accept-Encoding")))
        inner(h)

    with listener() as host:
        host.routes["/f"] = route
        url = host.url("/f")
        assert sourceio.size(url) == len(BODY)
        with sourceio.open_source(url) as f:
            assert f.read(3) == b"012"
        with sourceio.local_copy(url):
            pass
    assert seen == [("HEAD", "identity"), ("GET", "identity"), ("GET", "identity")]


def test_the_client_waits_30_seconds_and_checks_every_hop():
    with sourceio._client() as client:
        assert client.timeout == httpx.Timeout(30.0)
        assert client.headers["Accept-Encoding"] == "identity"
        assert client.follow_redirects is True
        assert client.event_hooks["request"] == [sourceio.egress.enforce_on_request_sync]


def test_a_server_ignoring_range_is_refused_from_the_second_byte():
    with listener() as host:
        host.routes["/f"] = serve(BODY, ranges=False)
        url = host.url("/f")
        with sourceio.open_source(url) as f:
            assert f.read(1) == b"0"  # positive control: a read from the start
            with pytest.raises(
                UpstreamUnavailableError,
                match=r"^\[UpstreamUnavailableError\] operate source: the server ignores "
                rf"range requests, so {re.escape(url)} can only be read whole",
            ):
                f.read(1)  # offset 1


def test_an_http_error_on_a_range_read_names_the_url():
    with listener() as host:
        host.routes["/boom"] = lambda h: status(h, 503)
        host.routes["/ok"] = serve(BODY)
        boom = host.url("/boom")
        with (
            pytest.raises(
                UpstreamUnavailableError,
                match=rf"^\[UpstreamUnavailableError\] operate source → HTTP 503 "
                rf"\({re.escape(boom)}\)$",
            ),
            sourceio.open_source(boom) as f,
        ):
            f.read(1)
        with sourceio.open_source(host.url("/ok")) as f:  # positive control
            assert f.read(1) == b"0"


def test_a_read_spanning_many_chunks_keeps_every_chunk():
    body = bytes(range(256)) * 1200  # 307,200 bytes: several socket reads
    with listener() as host:
        host.routes["/big"] = serve(body)
        with sourceio.open_source(host.url("/big")) as f:
            f.seek(7)
            assert f.read(200_000) == body[7:200_007]


def test_a_whole_file_answer_stops_at_the_bytes_asked_for():
    # A server that ignores Range and promises 100 bytes, sends 4, then hangs up: the read
    # from the start has its 4 bytes and must not wait for the rest.
    def route(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(BODY[:4])

    with listener() as host:
        host.routes["/short"] = route
        url = host.url("/short")
        with sourceio.open_source(url) as f:
            assert f.read(4) == b"0123"
        # Control: asking for one byte more reads on into the hang-up, a transport failure.
        with (
            pytest.raises(
                UpstreamUnavailableError,
                match=r"^\[UpstreamUnavailableError\] operate source transport failure: "
                rf"RemoteProtocolError\(.+\) \({re.escape(url)}\)$",
            ),
            sourceio.open_source(url) as f,
        ):
            f.read(5)


def test_a_remote_source_says_it_is_seekable_and_readable():
    # A reader that asks before seeking (a Parquet footer read) must be told yes.
    with listener() as host:
        host.routes["/f"] = serve(BODY)
        with sourceio.open_source(host.url("/f")) as f:
            assert f.seekable() is True
            assert f.readable() is True
            assert f.seek(4) == f.tell() == 4
            assert f.read(2) == b"45"
