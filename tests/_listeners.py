"""Real HTTP listeners on 127.0.0.1 for the operate egress tests.

Each listener records every request it receives, so a test can assert that a private
address was never contacted instead of inferring it from an error message.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Route = Callable[[BaseHTTPRequestHandler], None]


class Listener:
    def __init__(self, port: int) -> None:
        self.port = port
        self.hits: list[tuple[str, str, str | None]] = []  # (method, path, Range header)
        self.routes: dict[str, Route] = {}

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def status(h: BaseHTTPRequestHandler, code: int) -> None:
    h.send_response(code)
    h.send_header("Content-Length", "0")
    h.end_headers()


def serve(body: bytes, *, ranges: bool = True, length: bool = True, head: bool = True) -> Route:
    """Serve ``body``, honouring ``Range: bytes=a-b`` unless ``ranges`` is False."""

    def route(h: BaseHTTPRequestHandler) -> None:
        if h.command == "HEAD" and not head:
            return status(h, 405)
        code, data, extra = 200, body, {}
        wanted = h.headers.get("Range")
        if ranges and wanted and h.command == "GET":
            first, last = (int(x) for x in wanted.removeprefix("bytes=").split("-"))
            if first >= len(body):
                return status(h, 416)
            last = min(last, len(body) - 1)
            code, data = 206, body[first : last + 1]
            extra["Content-Range"] = f"bytes {first}-{last}/{len(body)}"
        h.send_response(code)
        for name, value in extra.items():
            h.send_header(name, value)
        if length:
            h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if h.command == "GET":
            h.wfile.write(data)
        return None

    return route


def redirect(location: str, *, head: Route | None = None) -> Route:
    """302 to ``location``; a HEAD goes to ``head`` instead when one is given, which is
    how a server answers the size probe honestly and redirects only the read."""

    def route(h: BaseHTTPRequestHandler) -> None:
        if h.command == "HEAD" and head is not None:
            return head(h)
        h.send_response(302)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return None

    return route


@contextlib.contextmanager
def listener() -> Iterator[Listener]:
    box: list[Listener] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def _answer(self) -> None:
            me = box[0]
            me.hits.append((self.command, self.path, self.headers.get("Range")))
            route = me.routes.get(self.path)
            if route is None:
                status(self, 404)
            else:
                route(self)

        do_GET = _answer
        do_HEAD = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    box.append(Listener(srv.server_address[1]))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield box[0]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
