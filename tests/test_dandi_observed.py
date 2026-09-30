"""DANDI behaviour the mutation run (nightly-guardrails, #88) showed no test observed.

148 mutants survived test_dandi.py. The largest gap: its mock server answered every
``/info/`` and ``/assets/`` path whatever the version, and the fixture's published and
draft versions share a name, so nothing showed that resolve reads the PUBLISHED version
(the one with a DOI). The rest: the request each call site sends, the error text a
failure carries, and record fields checked for presence rather than value.

The fixture shapes were checked against the live API (dandiset 000027) on 2026-09-30.
"""

from __future__ import annotations

import httpx
import pytest

from data_aggregator_mcp import _http, dandi
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Link

_API = "/api/dandisets/"
_PUB = "0.220126.1852"
_SEARCH = {
    "count": 2,
    "results": [
        {
            "identifier": "000003",
            "created": "2019-09-15T01:02:03Z",
            "modified": "2021-01-02T00:00:00Z",
            "most_recent_published_version": {"name": "Published name", "version": _PUB},
            "draft_version": {"name": "Draft name", "version": "draft"},
        },
        {"identifier": "000999", "draft_version": {"name": "Unpublished", "version": "draft"}},
    ],
}
_DETAIL = {
    "identifier": "000004",
    "created": "2020-03-16T21:48:04Z",
    "modified": "2021-08-12T00:00:00Z",
    "most_recent_published_version": {"name": "Published name", "version": _PUB},
    "draft_version": {"name": "Draft name", "version": "draft"},
}
_INFO = {
    "metadata": {
        "name": "Info name",
        "doi": f"10.48324/dandi.000004/{_PUB}",
        "license": ["spdx:CC-BY-4.0"],
        "contributor": [
            {"name": "Chandravadia, Nand", "roleName": ["dcite:Author"]},
            {"name": "NIH", "roleName": ["dcite:Funder"]},
        ],
        "url": f"https://dandiarchive.org/dandiset/000004/{_PUB}",
    }
}
_ASSETS = {
    "count": 3,
    "results": [
        {"path": "no-asset-id.nwb", "size": 1},
        {"asset_id": "aaa-111", "path": "sub-01/sub-01_ecephys.nwb", "size": 73156888},
        {"asset_id": "bbb-222"},
    ],
}
_FILES = [
    FileEntry(
        name="sub-01/sub-01_ecephys.nwb",
        size=73156888,
        url="https://api.dandiarchive.org/api/assets/aaa-111/download/",
        source="dandi",
    ),
    FileEntry(
        name="bbb-222",
        url="https://api.dandiarchive.org/api/assets/bbb-222/download/",
        source="dandi",
    ),
]


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


def _server(
    seen: list[httpx.Request],
    *,
    dandiset: dict = _DETAIL,
    version: str = _PUB,
    **answers: httpx.Response,
) -> httpx.MockTransport:
    """Search, dandiset 000004's detail (``dandiset``), and its version info and assets,
    which answer ONLY at ``version``: any other version path is a 404.
    ``answers`` overrides an endpoint ("search", "detail", "info", "assets")."""
    ident, ver = "000004", version
    routes = {
        _API: ("search", _SEARCH),
        f"{_API}{ident}/": ("detail", dandiset),
        f"{_API}{ident}/versions/{ver}/info/": ("info", _INFO),
        f"{_API}{ident}/versions/{ver}/assets/": ("assets", _ASSETS),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        kind, body = routes.get(request.url.path, ("", None))
        if kind in answers:
            return answers[kind]
        return httpx.Response(200, json=body) if kind else httpx.Response(404, json={})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# What each call site sends
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_call_is_a_json_get_at_the_published_version() -> None:
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen)) as c:
        await dandi.search(c, "mouse")
        await dandi.resolve(c, "dandi:000004")
    assert [(r.url.path, dict(r.url.params)) for r in seen] == [
        (_API, {"search": "mouse", "page_size": "10", "page": "1"}),
        (f"{_API}000004/", {}),
        (f"{_API}000004/versions/{_PUB}/info/", {}),
        (f"{_API}000004/versions/{_PUB}/assets/", {"page_size": "100", "metadata": "true"}),
    ]
    for r in seen:
        assert r.method == "GET"
        assert r.headers["Accept"] == "application/json"
        assert r.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 30.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dandiset",
    [{**_DETAIL, "most_recent_published_version": None}, {"identifier": "000004"}],
)
async def test_an_unpublished_dandiset_is_read_at_its_draft(dandiset: dict) -> None:
    version = "draft"
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen, dandiset=dandiset, version=version)) as c:
        r = await dandi.resolve(c, "dandi:000004")
    assert [q.url.path for q in seen[1:]] == [
        f"{_API}000004/versions/{version}/info/",
        f"{_API}000004/versions/{version}/assets/",
    ]
    assert r.files == _FILES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail", "service"),
    [
        ("search", "DANDI search"),
        ("detail", "DANDI resolve"),
        ("info", "DANDI version info"),
        ("assets", "DANDI assets"),
    ],
)
async def test_an_outage_is_tried_twice_and_named(fail: str, service: str, no_sleep: None) -> None:
    """Each call site gets MAX_RETRIES (2) attempts, not ``_http``'s default 3, and the
    error names which DANDI call failed. Positive control: nothing failing, both work."""
    async with httpx.AsyncClient(transport=_server([])) as c:
        assert (await dandi.search(c, "mouse"))[1]
        assert (await dandi.resolve(c, "dandi:000004")).files == _FILES

    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen, **{fail: httpx.Response(503)})) as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            if fail == "search":
                await dandi.search(c, "mouse")
            else:
                await dandi.resolve(c, "dandi:000004")
    assert str(err.value) == (
        f"[UpstreamUnavailableError] {service} exhausted 2 retries (last HTTP 503)"
    )
    assert len(seen) - len({r.url.path for r in seen}) == 1  # the failing call, twice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail", "service", "body"),
    [
        ("search", "DANDI search", b"[]"),
        ("assets", "DANDI assets", b"[]"),
        # Version info used to fall back to `info or {}`, so these two resolved to a
        # record with no metadata title, DOI, licence or authors, and no error.
        ("info", "DANDI version info", b"[]"),
        ("info", "DANDI version info", b"null"),
        # The dandiset detail read any JSON: `null` and `[]` were reported as "DANDI has
        # no dandiset" (an outage read as not-found), a non-empty list as AttributeError.
        ("detail", "DANDI resolve", b"null"),
        ("detail", "DANDI resolve", b"[]"),
        ("detail", "DANDI resolve", b'["000004"]'),
    ],
)
async def test_a_body_that_is_not_an_object_is_an_outage(
    fail: str, service: str, body: bytes, no_sleep: None
) -> None:
    """Search, the dandiset detail, the asset listing and version info each promise a
    JSON object; a 200 carrying anything else is a malformed body (retried, then
    raised), not an empty or missing result. (A 404 on the detail is still "no such
    dandiset", and a 404 on version info still the documented fallback.)"""
    async with httpx.AsyncClient(
        transport=_server([], **{fail: httpx.Response(200, content=body)})
    ) as c:
        with pytest.raises(UpstreamUnavailableError) as err:
            if fail == "search":
                await dandi.search(c, "mouse")
            else:
                await dandi.resolve(c, "dandi:000004")
    assert str(err.value).startswith(
        f"[UpstreamUnavailableError] {service} returned an unparseable 200 body after 2 tries"
    )


@pytest.mark.asyncio
async def test_resolve_errors_name_the_id() -> None:
    """Malformed, missing, and a detail with no identifier each say which id. Positive
    control: the same client resolves a present dandiset."""
    async with httpx.AsyncClient(transport=_server([])) as c:
        assert (await dandi.resolve(c, "dandi:000004")).id == "dandi:000004"
        with pytest.raises(NotFoundError) as malformed:
            await dandi.resolve(c, "dandi: ")
        with pytest.raises(NotFoundError) as missing:
            await dandi.resolve(c, "dandi:999999")
    blank = _server([], dandiset={**_DETAIL, "identifier": ""})
    async with httpx.AsyncClient(transport=blank) as c:
        with pytest.raises(NotFoundError) as unnamed:
            await dandi.resolve(c, "dandi:000004")
    assert str(malformed.value) == "[NotFoundError] malformed DANDI id 'dandi: '"
    assert str(missing.value) == "[NotFoundError] DANDI has no dandiset 999999"
    assert str(unnamed.value) == "[NotFoundError] DANDI has no dandiset 000004"


@pytest.mark.asyncio
async def test_resolve_trims_whitespace_around_the_id() -> None:
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen)) as c:
        r = await dandi.resolve(c, "dandi: 000004 ")
    assert seen[0].url.path == f"{_API}000004/"
    assert r.id == "dandi:000004"


@pytest.mark.asyncio
async def test_a_zero_page_size_still_asks_for_page_one() -> None:
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(seen)) as c:
        _, recs = await dandi.search(c, "mouse", size=0)
    assert seen[0].url.params["page"] == "1"
    assert len(recs) == 2


# --------------------------------------------------------------------------
# Records, field by field
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resolved_dandiset_in_full() -> None:
    """Title from the version info, then year, DOI, first licence, authors only, the
    info's own URL as landing page, and every asset with an id (one without is skipped)."""
    async with httpx.AsyncClient(transport=_server([])) as c:
        r = await dandi.resolve(c, "dandi:000004")
    assert r == DataResource(
        id="dandi:000004",
        source="dandi",
        kind="dataset",
        title="Info name",
        creators=[Creator(name="Chandravadia, Nand")],
        year=2020,
        doi=f"10.48324/dandi.000004/{_PUB}",
        license="spdx:CC-BY-4.0",
        access="open",
        last_updated="2021-08-12T00:00:00Z",
        files=_FILES,
        links=[
            Link(rel="landing_page", target_id=f"https://dandiarchive.org/dandiset/000004/{_PUB}")
        ],
    )


@pytest.mark.asyncio
async def test_missing_version_info_falls_back_without_inventing_values() -> None:
    """A 404 on version info degrades to the detail's own fields: the version's name,
    the plain landing page, no DOI, licence or authors; with no names, the id."""
    gone = {"info": httpx.Response(404, json={})}
    async with httpx.AsyncClient(transport=_server([], **gone)) as c:
        r = await dandi.resolve(c, "dandi:000004")
    assert (r.title, r.doi, r.license, r.creators) == ("Published name", None, None, [])
    assert r.links == [
        Link(rel="landing_page", target_id="https://dandiarchive.org/dandiset/000004")
    ]

    bare = _server([], dandiset={"identifier": "000004"}, version="draft", **gone)
    async with httpx.AsyncClient(transport=bare) as c:
        r = await dandi.resolve(c, "dandi:000004")
    assert (r.title, r.year, r.last_updated) == ("000004", None, None)


def test_search_listings_in_full() -> None:
    published, draft = _SEARCH["results"]
    assert dandi._normalize_listing(published) == DataResource(
        id="dandi:000003",
        source="dandi",
        kind="dataset",
        title="Published name",
        year=2019,
        last_updated="2021-01-02T00:00:00Z",
        links=[Link(rel="landing_page", target_id="https://dandiarchive.org/dandiset/000003")],
    )
    assert dandi._normalize_listing(draft) == DataResource(
        id="dandi:000999",
        source="dandi",
        kind="dataset",
        title="Unpublished",
        links=[Link(rel="landing_page", target_id="https://dandiarchive.org/dandiset/000999")],
    )
    # No identifier: nothing is invented in its place.
    assert dandi._normalize_listing({}).id == "dandi:"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (["spdx:CC-BY-4.0", "spdx:CC0-1.0"], "spdx:CC-BY-4.0"),
        ("spdx:CC0-1.0", "spdx:CC0-1.0"),
        ([], None),
        (None, None),
    ],
)
def test_license_takes_the_first_of_a_list_or_a_bare_string(raw: object, want: str | None) -> None:
    assert dandi._license(raw) == want  # type: ignore[arg-type]
