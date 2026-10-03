"""The requests the Dryad manifest sends, the fields it reads and the errors it names.

These pin behaviour that was already right (they pass on 8c70c70 as well); each kills a
mutant that survived the 2026-10-01 nightly run. The answers they use, and the probes
behind them, are in ``tests/test_dryad_answers.py``.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, dryad
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry
from tests.test_dryad_answers import (
    _API,
    _FILES_URL,
    _NOT_HELD,
    _NOT_SHOWN,
    _NYMPHS,
    _NYMPHS_ENTRY,
    _PKG,
    _PKG_DOI,
    _PKG_URL,
    _SHA_FILE,
    _dryad,
    _files,
    _page,
    live_only,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


# --- requests -------------------------------------------------------------------------


async def test_each_request_is_exactly_the_one_dryad_answers():
    """The dataset by its whole DOI percent-encoded (the unencoded path is a live 404),
    then its latest version's files 100 at a time, then each next link as given."""
    seen: list[httpx.Request] = []
    nxt = "/api/v2/versions/19295/files?page=2&per_page=100"
    routes = {
        _PKG_URL: _PKG,
        _FILES_URL: _page(_NYMPHS, total=2, nxt=nxt),
        f"https://datadryad.org{nxt}": _page(_SHA_FILE, total=2),
    }
    async with _dryad(routes, seen) as client:
        out = await dryad.files(client, _PKG_DOI)
    assert [(r.method, str(r.url)) for r in seen] == [
        ("GET", _PKG_URL),
        ("GET", _FILES_URL),
        ("GET", "https://datadryad.org/api/v2/versions/19295/files?page=2&per_page=100"),
    ]
    assert [f.name for f in out] == ["nymphs_replica 1.raw.zip", "0.03125_2to1_Solution.raw"]


async def test_a_doi_with_reserved_characters_stays_one_path_segment():
    """``/``, ``:``, ``?`` and ``#`` in a DOI are encoded, so the request names exactly
    that DOI and no other path, query or fragment."""
    seen: list[httpx.Request] = []
    url = f"{_API}/datasets/doi%3A10.5061%2Fdryad.x%2F1%3Fa%3D1%23f"
    async with _dryad({url: _NOT_SHOWN}, seen) as client:
        assert await dryad.files(client, "10.5061/dryad.x/1?a=1#f") == []
    assert [str(r.url) for r in seen] == [url]
    assert seen[0].url.query == b"" and seen[0].url.fragment == ""


# --- fields and errors --------------------------------------------------------------


async def test_each_file_field_is_read_from_its_own_key():
    sha = FileEntry(
        name="0.03125_2to1_Solution.raw",
        size=46680834,
        url="https://datadryad.org/downloads/file_stream/4356958",
        checksum="sha256:aa7ba327021e3929ea9348a836893196ed29969ae48069fbc371d18e9b934f2d",
    )
    bare: dict = {"_links": {}}
    no_type = {**_NYMPHS, "digestType": None}
    no_digest = {**_NYMPHS, "digest": None}
    out = await _files(
        {_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS, _SHA_FILE, bare, no_type, no_digest)}
    )
    assert out == [
        _NYMPHS_ENTRY,
        sha,  # "sha-256" is read as the hashlib name "sha256"
        FileEntry(name="", size=None, url="", checksum=None),
        _NYMPHS_ENTRY.model_copy(update={"checksum": None}),
        _NYMPHS_ENTRY.model_copy(update={"checksum": None}),
    ]


async def test_an_upstream_failure_names_the_dryad_call_that_failed():
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] Dryad dataset exhausted 3 retries \(last HTTP 503\)$",
    ):
        await _files({_PKG_URL: httpx.Response(503)})
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] Dryad files exhausted 3 retries \(last HTTP 503\)$",
    ):
        await _files({_PKG_URL: _PKG, _FILES_URL: httpx.Response(503)})
    # A version that vanished between the two calls is not "no files".
    with pytest.raises(
        NotFoundError, match=r'^\[NotFoundError\] Dryad files → HTTP 404: \{"error":"not-found"\}$'
    ):
        await _files({_PKG_URL: _PKG, _FILES_URL: _NOT_HELD})
    assert await _files({_PKG_URL: _PKG, _FILES_URL: _page(_NYMPHS)}) == [_NYMPHS_ENTRY]


# --- the walk -------------------------------------------------------------------------


def _walk(totals: list[int], pages: list[list[dict]]) -> dict[str, object]:
    """``len(pages)`` pages, each linking the next, page ``n`` reporting ``totals[n]``."""
    routes: dict[str, object] = {_PKG_URL: _PKG}
    url = _FILES_URL
    for n, files in enumerate(pages):
        nxt = None
        if n + 1 < len(pages):
            nxt = f"/api/v2/versions/19295/files?page={n + 2}&per_page=100"
        routes[url] = _page(*files, total=totals[n], nxt=nxt)
        url = f"https://datadryad.org{nxt}"
    return routes


def _f(n: int) -> dict:
    return {**_NYMPHS, "path": f"f{n}", "_links": {"self": {"href": f"/api/v2/files/{n}"}}}


async def test_the_walk_ends_at_the_page_cap_and_not_one_page_before(monkeypatch):
    monkeypatch.setattr(dryad, "_MAX_PAGES", 3)
    pages = [[_f(1)], [_f(2)], [_f(3)]]
    assert [f.name for f in await _files(_walk([3, 3, 3], pages))] == ["f1", "f2", "f3"]
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] Dryad files for 10\.5061/dryad\.50kt0: still unfinished after 3 pages; "
        r"refusing to return a partial manifest$",
    ):
        await _files(_walk([4] * 4, [*pages, [_f(4)]]))


async def test_a_walk_that_ends_short_of_or_past_the_total_raises():
    """The total is the last page's; a short walk and a walk with extra files both raise.
    Positive control: the same pages with a matching total."""
    pages = [[_f(1)], [_f(2)]]
    assert [f.name for f in await _files(_walk([9, 2], pages))] == ["f1", "f2"]
    for total in (3, 1):
        with pytest.raises(
            UpstreamUnavailableError,
            match=rf"^\[UpstreamUnavailableError\] Dryad files for 10\.5061/dryad\.50kt0: pagination ended with 2 of "
            rf"{total} files$",
        ):
            await _files(_walk([2, total], pages))
    assert await _files(_walk([0], [[]])) == []


async def test_a_next_link_back_to_a_read_page_raises():
    routes = _walk([2, 2], [[_f(1)], [_f(2)]])
    nxt = "https://datadryad.org/api/v2/versions/19295/files?page=2&per_page=100"
    routes[nxt] = _page(_f(2), total=2, nxt="/api/v2/versions/19295/files?per_page=100")
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] Dryad files for 10\.5061/dryad\.50kt0: pagination revisited "
        r"/api/v2/versions/19295/files\?per_page=100$",
    ):
        await _files(routes)


# --- live -----------------------------------------------------------------------------


@live_only
async def test_live_dryad_answers_a_doi_only_fully_percent_encoded():
    """What ``_ENCODE_ALL`` stands for: the same DOI with ``:`` and ``/`` left raw is a
    404. Positive control: the encoded path is the dataset. Through ``_http`` so that a
    429 (Dryad rate-limits bursts) is waited out rather than read as the answer."""

    async def get(url: str) -> dict | None:
        return await _http.request_json(
            client, "GET", url, service="Dryad dataset", expect=dict, not_found_returns=None
        )

    async with httpx.AsyncClient(timeout=60) as client:
        raw = await get(f"{_API}/datasets/doi:10.5061/dryad.50kt0")
        enc = await get(_PKG_URL)
    assert raw is None
    assert enc is not None and enc["identifier"] == "doi:10.5061/dryad.50kt0"
