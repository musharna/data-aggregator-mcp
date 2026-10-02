"""What the data.gov adapter sends and how it reads each field, pinned exactly.

Shapes from the live catalog (2026-10-02): see ``test_datagov_answers.py``.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import datagov
from data_aggregator_mcp.models import Creator
from tests.test_datagov_answers import _FULL


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)


def _recording(seen: list[httpx.Request], *bodies: dict) -> httpx.AsyncClient:
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call(r: httpx.Request) -> tuple[str, str, str | None, str | None]:
    return r.method, str(r.url), r.headers.get("accept"), r.headers.get("x-api-key")


_ONE = {"results": [_FULL], "after": "c1"}


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_the_catalog_is_sent():
    seen: list[httpx.Request] = []
    async with _recording(seen, _ONE, {"results": []}, {"results": [_FULL]}) as c:
        await datagov.search(c, "water quality", size=2)
        await datagov.resolve(c, "datagov:a/b c")
    assert [_call(r) for r in seen] == [
        (
            "GET",
            "https://catalog.data.gov/search?q=water+quality&per_page=2",
            "application/json",
            None,
        ),
        (
            "GET",
            "https://catalog.data.gov/search?q=water+quality&per_page=1&after=c1",
            "application/json",
            None,
        ),
        ("GET", "https://catalog.data.gov/api/dataset/a%2Fb%20c", "application/json", None),
    ]


@pytest.mark.asyncio
async def test_with_a_key_each_request_goes_to_the_gateway_with_the_key(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "k-123")
    seen: list[httpx.Request] = []
    async with _recording(seen, {"results": [_FULL]}) as c:
        await datagov.search(c, "water")
        await datagov.resolve(c, "datagov:water-quality")
    gw = "https://api.gsa.gov/technology/datagov/v4"
    assert [_call(r) for r in seen] == [
        ("GET", f"{gw}/search?q=water&per_page=10", "application/json", "k-123"),
        ("GET", f"{gw}/dataset/water-quality", "application/json", "k-123"),
    ]


@pytest.mark.asyncio
async def test_a_search_for_no_records_asks_nothing_and_reports_none():
    seen: list[httpx.Request] = []
    async with _recording(seen, _ONE) as c:
        assert await datagov.search(c, "water", size=0) == (0, [])
        # Positive control: one record asked for is one request, and more upstream.
        total, recs = await datagov.search(c, "water", size=1)
    assert (total, [r.id for r in recs], len(seen)) == (2, ["datagov:water-quality"], 1)


@pytest.mark.asyncio
async def test_an_empty_page_ends_the_walk_even_if_it_carries_a_cursor():
    seen: list[httpx.Request] = []
    async with _recording(seen, _ONE, {"results": [], "after": "c2"}) as c:
        total, recs = await datagov.search(c, "water", size=5)
    assert (total, len(recs), len(seen)) == (1, 1, 2)
    assert seen[1].url.params["after"] == "c1"  # positive control: the walk went on


def _hit(**dcat) -> dict:
    hit = copy.deepcopy(_FULL)
    hit["dcat"].update(dcat)
    return hit


def test_the_organization_is_the_author_else_the_publisher():
    assert datagov._normalize(_FULL).creators == [Creator(name="City of Somewhere")]
    for org in (None, {}, {"name": None}, {"name": "  "}):
        hit = {**_hit(), "organization": org}
        assert datagov._normalize(hit).creators == [Creator(name="Somewhere Water Board")]
        for pub in (None, {}, {"name": " "}):
            assert (
                datagov._normalize({**hit, "dcat": {**hit["dcat"], "publisher": pub}}).creators
                == []
            )
    padded = {**_hit(publisher={"name": " Board "}), "organization": None}
    assert datagov._normalize(padded).creators == [Creator(name="Board")]


def test_title_and_description_fall_back_to_the_catalog_fields():
    full = datagov._normalize(_FULL)
    assert (full.title, full.description) == ("Water quality", "Samples")
    bare = datagov._normalize(_hit(title=None, description=""))
    assert (bare.title, bare.description) == ("Hit title", "Hit description")
    none = datagov._normalize({"slug": "s", "dcat": {}})
    assert (none.id, none.title, none.description) == ("datagov:s", "", None)


def test_each_distribution_with_a_url_is_a_file_named_by_what_it_carries():
    dists = [
        {"title": "no url"},
        {"downloadURL": "https://x.gov/d/a.csv", "accessURL": "https://x.gov/d", "title": "A"},
        {"accessURL": "https://x.gov/d/landing", "format": "API", "mediaType": ""},
        {"downloadURL": "https://x.gov/d/b.json", "mediaType": "application/json"},
        {"downloadURL": "https://x.gov/d/"},
    ]
    files = datagov._files({"distribution": dists})
    assert [(f.name, f.url, f.mime, f.source, f.checksum) for f in files] == [
        ("A", "https://x.gov/d/a.csv", None, "datagov", None),
        ("API", "https://x.gov/d/landing", None, "datagov", None),
        ("b.json", "https://x.gov/d/b.json", "application/json", "datagov", None),
        ("distribution", "https://x.gov/d/", None, "datagov", None),
    ]


@pytest.mark.parametrize(
    ("url", "spdx"),
    [
        ("http://www.opendefinition.org/licenses/odc-odbl", "ODbL-1.0"),
        ("http://www.opendefinition.org/licenses/odc-pddl", "PDDL-1.0"),
        ("https://opendefinition.org/licenses/cc-zero/", "CC0-1.0"),  # live, trailing slash
        ("http://www.OpenDefinition.org/licenses/ODC-BY", "ODC-By-1.0"),
        ("http://www.opendefinition.org/licenses/cc-by-4.0", "CC-BY-4.0"),  # via normalize_spdx
        ("http://www.opendefinition.org/licenses/gfdl", None),
        (" https://creativecommons.org/licenses/by/4.0/ ", "CC-BY-4.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("", None),
        (None, None),
    ],
)
def test_a_license_url_is_read_as_its_spdx_id(url, spdx):
    assert datagov._license({"license": url}) == spdx


@pytest.mark.parametrize(
    ("level", "access"),
    [
        ("public", "open"),
        (" Public ", "open"),
        ("restricted public", "restricted"),
        ("Non-Public", "closed"),
        ("secret", None),
        ("", None),
        (None, None),
    ],
)
def test_access_level_is_mapped_and_never_guessed(level, access):
    assert datagov._normalize(_hit(accessLevel=level)).access == access
