"""The Kaggle adapter against verbatim answers of its public API (captured 2026-10-17),
and live.

Round 3 T4 (labelled honey bee images): six key datasets were held only on Kaggle, with
no DOI, so no source reached them; Kaggle's own search returns all six.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import kaggle, router
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError

_FIX = Path(__file__).parent / "fixtures"
LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
# datasets/list?search=varroa: a full page of 20, then a page of 5.
_P1 = json.loads((_FIX / "kaggle_search_varroa_p1.json").read_text())
_P2 = json.loads((_FIX / "kaggle_search_varroa_p2.json").read_text())
# The answer to a word no dataset holds.
_EMPTY = json.loads((_FIX / "kaggle_search_empty.json").read_text())
# datasets/view/masuel/bee-hyperspectral-dataset (three versions).
_VIEW = json.loads((_FIX / "kaggle_view_bee_hyperspectral.json").read_text())
# datasets/view of a dataset that does not exist: HTTP 403.
_MISSING = (_FIX / "kaggle_view_missing.json").read_text()
_LIST = re.compile(r"https://www\.kaggle\.com/api/v1/datasets/list\?.*")


def _pages(httpx_mock: HTTPXMock, pages: dict[int, list]) -> list[dict]:
    """Answer datasets/list by page number; returns the params of each request sent."""
    sent: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        sent.append(params)
        return httpx.Response(200, json=pages.get(int(params["page"]), []))

    httpx_mock.add_callback(answer, url=_LIST, is_reusable=True)
    return sent


async def test_search_reads_a_page_of_datasets(httpx_mock: HTTPXMock) -> None:
    sent = _pages(httpx_mock, {1: _P1})
    async with httpx.AsyncClient() as c:
        total, recs = await kaggle.search(c, "varroa", size=20)
    assert sent == [{"search": "varroa", "page": "1"}]
    assert len(recs) == 20 and recs[0].id == "kaggle:jenny18/honey-bee-annotated-images"
    r = next(r for r in recs if r.id == "kaggle:imonbilk/bee-dataset-but-hs")
    assert r.source == "kaggle" and r.kind == "dataset" and r.access == "open"
    assert r.links[0].target_id == "https://www.kaggle.com/datasets/imonbilk/bee-dataset-but-hs"
    # The list holds no description and no versions: the subtitle stands in, no year.
    assert r.description and r.year is None
    assert r.metrics is not None and r.metrics.downloads is not None
    # A full page with no total from Kaggle: one more than it held, so a cursor goes on.
    assert total == 21


async def test_an_offset_is_read_from_its_page_and_the_last_page_ends_the_total(
    httpx_mock: HTTPXMock,
) -> None:
    """Kaggle pages by 20 whatever size is asked: offset 15 is rows 15-19 of page 1, and
    offset 22 rows 2-4 of page 2, whose 5 rows end the list (total exact: 25)."""
    sent = _pages(httpx_mock, {1: _P1, 2: _P2})
    async with httpx.AsyncClient() as c:
        total1, first = await kaggle.search(c, "varroa", size=10, offset=15)
        total2, last = await kaggle.search(c, "varroa", size=10, offset=22)
        total3, beyond = await kaggle.search(c, "varroa", size=10, offset=40)
    assert [p["page"] for p in sent] == ["1", "2", "3"]
    assert [r.id for r in first] == [f"kaggle:{d['ref']}" for d in _P1[15:20]]
    assert total1 == 21
    assert [r.id for r in last] == [f"kaggle:{d['ref']}" for d in _P2[2:5]]
    assert total2 == 25
    assert (total3, beyond) == (40, [])


async def test_a_router_walk_lists_every_dataset_once(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """The lower-bound total keeps the cursor going past a full page, and the short page
    ends it: 25 datasets, each once."""
    _pages(httpx_mock, {1: _P1, 2: _P2})
    monkeypatch.setattr(router, "_ADAPTERS", {"kaggle": kaggle})
    seen: list[str] = []
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="varroa", sources=["kaggle"], size=10)
        while True:
            seen += [r.id for r in page.results]
            if not page.next_cursor:
                break
            page = await router.search_page(c, cursor=page.next_cursor)
    assert sorted(seen) == sorted(f"kaggle:{d['ref']}" for d in _P1 + _P2)
    assert len(seen) == len(set(seen)) == 25


async def test_the_router_names_kaggle_when_every_word_empties_it(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    sent = _pages(httpx_mock, {1: _EMPTY})
    monkeypatch.setattr(router, "_ADAPTERS", {"kaggle": kaggle})
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="varroa honeybee xyzzyqq", sources=["kaggle"])
        assert page.total == 0 and page.errors["all_words"].startswith("kaggle matched nothing")
        # Positive control: a word it holds is no note.
        httpx_mock.reset()
        _pages(httpx_mock, {1: _P2})
        page = await router.search_page(c, query="varroa", sources=["kaggle"])
    assert "all_words" not in page.errors and page.count == 5
    assert sent[0]["search"] == "varroa honeybee xyzzyqq"


async def test_resolve_reads_the_dataset_and_its_first_version_year(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://www.kaggle.com/api/v1/datasets/view/masuel/bee-hyperspectral-dataset",
        json=_VIEW,
    )
    async with httpx.AsyncClient() as c:
        r = await kaggle.resolve(c, "kaggle:masuel/bee-hyperspectral-dataset")
    assert r.id == "kaggle:masuel/bee-hyperspectral-dataset" and r.title == "Bee Dataset BUT-HS2"
    # Versions 2025-03-11, 2025-04-01, 2025-06-02: the year is the first's.
    assert r.year == 2025 and r.last_updated == _VIEW["lastUpdated"]
    assert r.description and r.description.startswith("Our dataset consists of 647 photos")
    assert r.license == "LGPL-3.0" and r.creators[0].name == "Masuel"


async def test_resolve_keeps_kaggles_spelling_of_the_ref_and_no_other_dataset(
    httpx_mock: HTTPXMock,
) -> None:
    """Kaggle reads a ref in any case and answers with its own (live: Masuel/Bee-... is
    masuel/bee-...); an answer naming another dataset is not the one asked for."""
    httpx_mock.add_response(
        url="https://www.kaggle.com/api/v1/datasets/view/Masuel/Bee-Hyperspectral-Dataset",
        json=_VIEW,
    )
    other = "https://www.kaggle.com/api/v1/datasets/view/masuel/bee-dataset-but-hs3"
    httpx_mock.add_response(url=other, json=_VIEW)
    async with httpx.AsyncClient() as c:
        r = await kaggle.resolve(c, "kaggle:Masuel/Bee-Hyperspectral-Dataset")
        assert r.id == "kaggle:masuel/bee-hyperspectral-dataset"
        with pytest.raises(NotFoundError, match="answered 'masuel/bee-hyperspectral-dataset'"):
            await kaggle.resolve(c, "kaggle:masuel/bee-dataset-but-hs3")


async def test_an_unknown_dataset_is_not_found_and_other_refusals_are_trouble(
    httpx_mock: HTTPXMock,
) -> None:
    """Kaggle answers a dataset that does not exist with 403 "Permission 'datasets.get'
    was denied"; any other 403 or a 500 is the upstream failing, not "not found"."""
    url = "https://www.kaggle.com/api/v1/datasets/view/nobody-xx/no-such-dataset-zz"
    httpx_mock.add_response(url=url, status_code=403, text=_MISSING)
    async with httpx.AsyncClient() as c:
        with pytest.raises(NotFoundError, match="no public dataset nobody-xx/no-such-dataset-zz"):
            await kaggle.resolve(c, "kaggle:nobody-xx/no-such-dataset-zz")
        httpx_mock.add_response(url=url, status_code=403, text='{"code":403,"message":"Forbidden"}')
        with pytest.raises(UpstreamUnavailableError, match="HTTP 403"):
            await kaggle.resolve(c, "kaggle:nobody-xx/no-such-dataset-zz")
        with pytest.raises(NotFoundError, match="malformed Kaggle dataset id"):
            await kaggle.resolve(c, "kaggle:no-slash")
        # Positive control: the verbatim dataset resolves.
        httpx_mock.add_response(
            url="https://www.kaggle.com/api/v1/datasets/view/masuel/bee-hyperspectral-dataset",
            json=_VIEW,
        )
        assert (await kaggle.resolve(c, "kaggle:masuel/bee-hyperspectral-dataset")).year == 2025


async def test_a_bare_403_is_kaggle_refusing_the_caller(httpx_mock: HTTPXMock) -> None:
    """GitHub's runners got a 403 with no body for every Kaggle call on 2026-10-18 while
    the same calls were answered elsewhere: the host not serving us, so ``down``. A 403
    that says something is an answer and stays not ``down``; "datasets.get" stays not
    found."""
    view = "https://www.kaggle.com/api/v1/datasets/view/masuel/bee-hyperspectral-dataset"
    async with httpx.AsyncClient() as c:
        httpx_mock.add_response(url=_LIST, status_code=403, text="")
        with pytest.raises(UpstreamUnavailableError, match="refused this client") as e:
            await kaggle.search(c, "varroa")
        assert e.value.down
        httpx_mock.add_response(url=view, status_code=403, text="")
        with pytest.raises(UpstreamUnavailableError, match="refused this client") as e:
            await kaggle.resolve(c, "kaggle:masuel/bee-hyperspectral-dataset")
        assert e.value.down
        # Controls: a 403 with a body is an answer, not the host down ...
        httpx_mock.add_response(
            url=view, status_code=403, text='{"code":403,"message":"Forbidden"}'
        )
        with pytest.raises(UpstreamUnavailableError, match="HTTP 403") as e:
            await kaggle.resolve(c, "kaggle:masuel/bee-hyperspectral-dataset")
        assert not e.value.down
        # ... "datasets.get" is still a dataset Kaggle does not have ...
        httpx_mock.add_response(url=view, status_code=403, text=_MISSING)
        with pytest.raises(NotFoundError):
            await kaggle.resolve(c, "kaggle:masuel/bee-hyperspectral-dataset")
        # ... and an answer is read.
        httpx_mock.add_response(url=_LIST, json=_P1)
        assert len((await kaggle.search(c, "varroa", size=20))[1]) == 20


def test_the_year_is_the_first_versions_not_the_latest() -> None:
    """``lastUpdated`` and the newest version date the latest upload; a dataset first
    published in 2019 and updated in 2025 is from 2019. Positive control: one version."""
    versions = [
        {"versionNumber": n, "creationDate": d} for n, d in ((2, "2025-06-02"), (1, "2019-03-11"))
    ]
    assert kaggle._normalize(_with(_VIEW, versions=versions)).year == 2019
    assert kaggle._normalize(_with(_VIEW, versions=versions[:1])).year == 2025


def _with(row: dict, **fields) -> dict:
    out = copy.deepcopy(row)
    out.update(fields)
    return out


_MALFORMED = [
    pytest.param({"page": 1}, id="not-a-list"),
    pytest.param([_with(_P2[0], ref="no slash")], id="ref-not-owner-slug"),
    pytest.param([_with(_P2[0], title=None)], id="title-missing"),
    pytest.param([_with(_P2[0], licenseName=4)], id="licence-not-a-string"),
    pytest.param([_with(_P2[0], downloadCount="12")], id="downloads-not-an-int"),
    pytest.param([_with(_P2[0], voteCount=True)], id="votes-a-bool"),
    pytest.param([_with(_P2[0], tags=["image"])], id="tag-not-an-object"),
    pytest.param([_with(_P2[0], versions=[{"creationDate": 2025}])], id="version-date-not-text"),
]


@pytest.mark.parametrize("body", _MALFORMED)
async def test_an_answer_of_the_wrong_shape_fails_the_search(httpx_mock: HTTPXMock, body) -> None:
    httpx_mock.add_response(url=_LIST, json=body, is_reusable=True)
    async with httpx.AsyncClient() as c:
        with pytest.raises(UpstreamUnavailableError, match="Kaggle search"):
            await kaggle.search(c, "varroa")
        # Positive control: the verbatim page is read.
        httpx_mock.reset()
        httpx_mock.add_response(url=_LIST, json=_P2)
        assert len((await kaggle.search(c, "varroa"))[1]) == 5


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("CC0: Public Domain", "CC0-1.0"),
        ("Attribution 4.0 International (CC BY 4.0)", "CC-BY-4.0"),
        ("CC BY-SA 4.0", "CC-BY-SA-4.0"),
        ("GPL 2", "GPL-2.0"),
        ("GNU Lesser General Public License 3.0", "LGPL-3.0"),
        ("Apache 2.0", "Apache-2.0"),
        ("Attribution 3.0 IGO (CC BY 3.0 IGO)", "CC-BY-3.0-IGO"),
        ("Other (specified in description)", "Other (specified in description)"),
        ("Unknown", None),
        (None, None),
    ],
)
def test_a_kaggle_licence_name_is_read_as_spdx_or_kept_as_written(name, want) -> None:
    assert kaggle._license(name) == want


# --- live -----------------------------------------------------------------------------

# Round 3 T4 key datasets held only on Kaggle (refs from the task's keys).
_T4 = {
    "S1": "ivanfel/honey-bee-pollen",
    "S5": "alcor-lab/ev2-image-dataset",
    "S7": "kport354041/honeybee-positions",
    "S19": "ashfaqsyed/bees-dataset",
    "S20": "jonathanbyrne/to-bee-or-not-to-bee",
    "S26": "rowlettheowlet/bee-detection-from-video-frames",
}


@live_only
async def test_live_the_round_3_kaggle_only_datasets_are_found() -> None:
    found: set[str] = set()
    async with httpx.AsyncClient(timeout=60) as c:
        for q in ("honey bee", "bee varroa"):
            page = await router.search_page(c, query=q, sources=["kaggle"], size=20)
            ids = {r.id for r in page.results}
            if page.next_cursor:
                ids |= {
                    r.id for r in (await router.search_page(c, cursor=page.next_cursor)).results
                }
            found |= {k for k, ref in _T4.items() if f"kaggle:{ref}" in ids}
            assert "kaggle" not in page.errors, page.errors
    assert found == set(_T4), sorted(set(_T4) - found)


@live_only
async def test_live_resolve_of_the_advertised_example_and_an_unknown_id() -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await kaggle.resolve(c, "kaggle:ivanfel/honey-bee-pollen")
        assert r.title == "Honey Bee pollen" and r.license == "CC-BY-SA-4.0" and r.year == 2018
        with pytest.raises(NotFoundError):
            await kaggle.resolve(c, "kaggle:nobody-xx/no-such-dataset-zz")


@live_only
async def test_live_an_igo_licence_resolves_as_its_own_spdx_id() -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        # WHO life expectancy, "Attribution-NonCommercial-ShareAlike 3.0 IGO (CC BY-NC-SA 3.0 IGO)".
        igo = await kaggle.resolve(c, "kaggle:joebeachcapital/life-expectancy")
        # Positive control: an unported licence on the same path.
        plain = await kaggle.resolve(c, "kaggle:ivanfel/honey-bee-pollen")
    assert (igo.license, plain.license) == ("CC-BY-NC-SA-3.0-IGO", "CC-BY-SA-4.0")
