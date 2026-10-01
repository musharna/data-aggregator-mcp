"""What `_http` does that its callers could see, pinned (#88 burn-down of `_http`).

The adapters' tests drive every request through `_http`, but they stub the network
at the response and rarely read what was sent or how long a retry waited, so a
backoff that stopped doubling, a timeout that never reached httpx, or a wrapper that
dropped `data=` survived the mutation run (76 of 221 mutants). These tests drive the
three public wrappers against a server that records each request.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError, RateLimitError, UpstreamUnavailableError

_URL = "https://x.test/r"
_LONG = "x" * 300  # longer than the 200 characters an error message quotes


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every delay `_http` sleeps, in order; nothing really sleeps."""
    seen: list[float] = []

    async def _record(d: float, *_a: object) -> None:
        seen.append(d)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)
    return seen


class _Server:
    """Answers with ``replies`` in turn (the last repeats) and records each request."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def test_backoff_doubles_from_one_second_for_every_retried_failure(
    waits: list[float],
) -> None:
    """A transport error, an unparseable 200 and a 5xx without Retry-After all wait
    1, 2, 4 s, and the last try raises without waiting."""
    cases = [
        (httpx.ConnectError("boom"), "t unreachable after 4 tries: ConnectError('boom')"),
        (httpx.Response(503), "t exhausted 4 retries (last HTTP 503)"),
    ]
    for reply, message in cases:
        waits.clear()
        server = _Server(reply)
        async with server.client() as c:
            with pytest.raises(UpstreamUnavailableError) as err:
                await _http.request_with_retry(c, "GET", _URL, service="t", max_retries=4)
        assert err.value.args == (message,)
        assert waits == [1.0, 2.0, 4.0]
        assert len(server.requests) == 4

    waits.clear()
    server = _Server(httpx.Response(200, text="{bad"))
    async with server.client() as c:
        with pytest.raises(UpstreamUnavailableError, match="unparseable 200 body after 4 tries"):
            await _http.request_json(c, "GET", _URL, service="t", max_retries=4, expect=dict)
    assert waits == [1.0, 2.0, 4.0]


async def test_a_retry_that_then_succeeds_waits_only_before_it(waits: list[float]) -> None:
    server = _Server(httpx.Response(502), httpx.Response(200, json={"ok": 1}))
    async with server.client() as c:
        got = await _http.request_json(c, "GET", _URL, service="t", expect=dict)
    assert got == {"ok": 1}
    assert waits == [1.0]


async def test_exhausted_429_is_a_rate_limit_not_an_outage(waits: list[float]) -> None:
    """Positive control in the same test: an exhausted 503 is an outage."""
    for status, kind, message in [
        (429, RateLimitError, "t exhausted 2 retries (HTTP 429)"),
        (503, UpstreamUnavailableError, "t exhausted 2 retries (last HTTP 503)"),
    ]:
        async with _Server(httpx.Response(status)).client() as c:
            with pytest.raises(kind) as err:
                await _http.request_with_retry(c, "GET", _URL, service="t", max_retries=2)
        assert type(err.value) is kind
        assert err.value.args == (message,)


async def test_a_terminal_status_is_not_retried_and_quotes_200_characters(
    waits: list[float],
) -> None:
    for status, kind in [(404, NotFoundError), (418, UpstreamUnavailableError)]:
        server = _Server(httpx.Response(status, text=_LONG))
        async with server.client() as c:
            with pytest.raises(kind) as err:
                await _http.request_with_retry(c, "GET", _URL, service="t")
        assert err.value.args == (f"t → HTTP {status}: {'x' * 200}",)
        assert len(server.requests) == 1
    assert waits == []


async def test_a_300_is_an_error_status_not_a_success() -> None:
    """2xx is success; 300 Multiple Choices (no Location, so not followed) is not."""
    async with _Server(httpx.Response(300, text="choose")).client() as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            await _http.request_with_retry(c, "GET", _URL, service="t")
        assert err.value.args == ("t → HTTP 300: choose",)
    async with _Server(httpx.Response(299, text="ok")).client() as c:
        assert (await _http.request_with_retry(c, "GET", _URL, service="t")).text == "ok"


async def test_max_retries_counts_tries_and_must_be_at_least_one(waits: list[float]) -> None:
    server = _Server(httpx.Response(503))
    async with server.client() as c:
        with pytest.raises(ValueError, match="max_retries must be at least 1, got 0"):
            await _http.request_with_retry(c, "GET", _URL, service="t", max_retries=0)
        assert server.requests == []
        with pytest.raises(UpstreamUnavailableError, match="exhausted 1 retries"):
            await _http.request_with_retry(c, "GET", _URL, service="t", max_retries=1)
    assert len(server.requests) == 1
    assert waits == []


@pytest.mark.parametrize("wrapper", ["request_with_retry", "request_xml", "request_json"])
async def test_each_wrapper_sends_what_it_is_given(wrapper: str) -> None:
    """method, params, data, headers and timeout reach httpx unchanged."""
    json = wrapper == "request_json"
    server = _Server(
        httpx.Response(200, json={"ok": 1}) if json else httpx.Response(200, text="<ok/>")
    )
    kwargs = {"expect": object} if json else {}
    async with server.client() as c:
        await getattr(_http, wrapper)(
            c,
            "POST",
            _URL,
            service="t",
            params={"q": "1"},
            data={"k": "v"},
            headers={"X-Test": "yes"},
            timeout=12.5,
            **kwargs,
        )
    (sent,) = server.requests
    assert sent.method == "POST"
    assert sent.url.params["q"] == "1"
    assert sent.content == b"k=v"
    assert sent.headers["X-Test"] == "yes"
    assert sent.extensions["timeout"] == {
        "connect": 12.5,
        "read": 12.5,
        "write": 12.5,
        "pool": 12.5,
    }


@pytest.mark.parametrize(
    "wrapper", ["request_with_retry", "request_xml", "request_json", "request_json_with_headers"]
)
async def test_each_wrapper_defaults_to_30_seconds_and_3_tries(
    wrapper: str, waits: list[float]
) -> None:
    server = _Server(httpx.Response(503))
    kwargs = {"expect": object} if wrapper.startswith("request_json") else {}
    async with server.client() as c:
        with pytest.raises(
            UpstreamUnavailableError, match=r"^\[UpstreamUnavailableError\] svc exhausted 3 retries"
        ):
            await getattr(_http, wrapper)(c, "GET", _URL, service="svc", **kwargs)
    assert len(server.requests) == 3
    assert {r.extensions["timeout"]["read"] for r in server.requests} == {30.0}


@pytest.mark.parametrize(
    "wrapper", ["request_with_retry", "request_xml", "request_json", "request_json_with_headers"]
)
async def test_each_wrapper_follows_a_redirect_by_default(wrapper: str) -> None:
    """httpx's AsyncClient does NOT follow redirects unless told to; `_http` does."""
    server = _Server(
        httpx.Response(302, headers={"Location": "https://x.test/there"}),
        httpx.Response(200, json={"ok": 1}),
    )
    kwargs = {"expect": dict} if wrapper.startswith("request_json") else {}
    if wrapper == "request_xml":
        server.replies[1] = httpx.Response(200, text="<ok/>")
    async with server.client() as c:
        await getattr(_http, wrapper)(c, "GET", _URL, service="t", **kwargs)
    assert [str(r.url) for r in server.requests] == [_URL, "https://x.test/there"]


async def test_follow_redirects_false_returns_the_redirect_itself() -> None:
    server = _Server(httpx.Response(303, headers={"Location": "https://x.test/there"}))
    async with server.client() as c:
        resp = await _http.request_with_retry(c, "GET", _URL, service="t", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["Location"] == "https://x.test/there"
    assert len(server.requests) == 1


async def test_request_xml_passes_not_found_returns() -> None:
    """Positive control: without it, a 404 is NotFoundError."""
    sentinel = object()
    async with _Server(httpx.Response(404)).client() as c:
        assert (
            await _http.request_xml(c, "GET", _URL, service="t", not_found_returns=sentinel)
            is sentinel
        )
        with pytest.raises(NotFoundError):
            await _http.request_xml(c, "GET", _URL, service="t")


def test_wrong_shape_message_names_every_accepted_type_and_quotes_200_characters() -> None:
    parse = _http._json_parser((dict, list), None)
    with pytest.raises(_http.UnexpectedShapeError) as err:
        parse(httpx.Response(200, json=_LONG))
    assert err.value.args == (f'expected dict or list JSON, got str: "{"x" * 199}',)
    assert parse(httpx.Response(200, json=[1])) == [1]


def test_doi_path_keeps_slashes_and_encodes_every_other_reserved_character() -> None:
    assert _http.doi_path("10.1002/(SICI)1097-4636#?;<>") == (
        "10.1002/%28SICI%291097-4636%23%3F%3B%3C%3E"
    )


async def test_request_json_with_headers_returns_the_checked_body_and_the_headers(
    waits: list[float],
) -> None:
    """The body goes through the same `expect` check and retry as request_json."""
    ok = _Server(httpx.Response(200, json={"n": 1}, headers={"X-Total": "9"}))
    async with ok.client() as c:
        body, headers = await _http.request_json_with_headers(
            c, "GET", _URL, service="t", expect=dict
        )
    assert body == {"n": 1}
    assert headers["x-total"] == "9"
    async with _Server(httpx.Response(200, json=[1])).client() as c:
        with pytest.raises(UpstreamUnavailableError, match="expected dict JSON, got list"):
            await _http.request_json_with_headers(c, "GET", _URL, service="t", expect=dict)
