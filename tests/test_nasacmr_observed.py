"""What the NASA CMR adapter sends and how it reads each field (#88 mutation burn-down).

Each test pins a value, not a presence: the exact request, the exact error text, and
the field each UMM-C leaf lands in, beside the shape that must not be confused with it.
"""

import copy

import httpx
import pytest

from data_aggregator_mcp import _http, nasacmr
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import Creator, Link
from tests.test_nasacmr_answers import _FULL, _page


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(body: object, sent: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _umm(**fields) -> dict:
    return {"meta": {"concept-id": "C1-PROV"}, "umm": fields}


# A collection with nothing optional, each null as the live sample served it
# (2026-10-02: UseConstraints null for 789 of 1,750, RelatedUrls 182, DataDates 1,064,
# and 1,146 DOI objects give a MissingReason instead of a DOI). A null Term was not seen
# live; it drives the Topic fallback.
_SPARSE = {
    "meta": {"concept-id": "C1214470488-ASF", "revision-date": "2025-01-01T00:00:00Z"},
    "umm": {
        "EntryTitle": "Sparse collection",
        "Abstract": "A",
        "DOI": {"MissingReason": "Not Applicable", "Explanation": "None assigned."},
        "DataCenters": [{"ShortName": "ASF"}],
        "ScienceKeywords": [{"Category": "EARTH SCIENCE", "Topic": "LAND SURFACE", "Term": None}],
        "UseConstraints": None,
        "RelatedUrls": None,
        "DataDates": None,
    },
}


# --- requests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_sends_exactly_keyword_page_size_and_offset():
    sent: list[httpx.Request] = []
    async with _recording(_page(), sent) as c:
        await nasacmr.search(c, "sea ice")
        await nasacmr.search(c, "sea ice", size=7, offset=20)
    assert [r.method for r in sent] == ["GET", "GET"]
    assert {str(r.url.copy_with(query=None)) for r in sent} == {nasacmr.SEARCH}
    assert [dict(r.url.params) for r in sent] == [
        {"keyword": "sea ice", "page_size": "10", "offset": "0"},
        {"keyword": "sea ice", "page_size": "7", "offset": "20"},
    ]


@pytest.mark.asyncio
async def test_resolve_sends_exactly_the_concept_id_and_reads_the_whole_record():
    sent: list[httpx.Request] = []
    async with _recording(_page(_FULL), sent) as c:
        r = await nasacmr.resolve(c, "nasacmr:C2586786218-POCLOUD")
    assert [(q.method, str(q.url.copy_with(query=None)), dict(q.url.params)) for q in sent] == [
        ("GET", nasacmr.SEARCH, {"concept_id": "C2586786218-POCLOUD"})
    ]
    assert r.model_dump(exclude_defaults=True) == {
        "id": "nasacmr:C2586786218-POCLOUD",
        "source": "nasacmr",
        "kind": "dataset",
        "title": _FULL["umm"]["EntryTitle"],
        "creators": [{"name": "NASA/JPL/PODAAC"}, {"name": "UK/MOD/MET"}],
        "year": 2013,
        "description": _FULL["umm"]["Abstract"],
        "doi": "10.5067/GHOST-4RM02",
        "subjects": ["SEA ICE", "OCEAN TEMPERATURE"],
        "license": "CC-BY-4.0",
        "access": "open",
        "links": [
            {
                "rel": "data_access",
                "target_id": "https://search.earthdata.nasa.gov/search/granules?p=C2586786218-POCLOUD",
            }
        ],
        "last_updated": "2026-09-28T14:24:52.636Z",
    }


@pytest.mark.asyncio
async def test_resolve_of_an_unknown_id_names_it():
    async with _recording(_page(_FULL), []) as c:  # positive control
        assert (await nasacmr.resolve(c, "C2586786218-POCLOUD")).doi == "10.5067/GHOST-4RM02"
    async with _recording(_page(hits=0), []) as c:
        with pytest.raises(NotFoundError) as info:
            await nasacmr.resolve(c, "nasacmr:C0-NONE")
    assert info.value.args == ("NASA CMR has no collection 'C0-NONE'",)


@pytest.mark.asyncio
async def test_a_sparse_live_shaped_collection_passes_and_reads_as_absent():
    async with _recording(_page(_SPARSE, _FULL, hits=2), []) as c:
        total, recs = await nasacmr.search(c, "x")
    assert total == 2
    r = recs[0]
    assert (r.doi, r.license, r.access, r.year, r.links) == (None, None, None, None, [])
    assert r.subjects == ["LAND SURFACE"] and r.creators == [Creator(name="ASF")]
    assert r.title == "Sparse collection" and r.description == "A"
    assert r.last_updated == "2025-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_search_hits_are_compacted_resolve_is_not():
    long = _with_abstract("word " * 400)
    async with _recording(_page(long), []) as c:
        _t, [hit] = await nasacmr.search(c, "x")
        full = await nasacmr.resolve(c, "nasacmr:C2586786218-POCLOUD")
    assert full.description == long["umm"]["Abstract"]
    assert hit.description is not None and long["umm"]["Abstract"].startswith(hit.description)
    assert len(hit.description) < len(full.description)


def _with_abstract(text: str) -> dict:
    item = copy.deepcopy(_FULL)
    item["umm"]["Abstract"] = text
    return item


# --- field readers ------------------------------------------------------------


def test_title_is_the_entry_title_or_empty():
    assert nasacmr._normalize(_umm(EntryTitle="T")).title == "T"
    assert nasacmr._normalize(_umm(EntryTitle=None)).title == ""
    assert nasacmr._normalize(_umm()).title == ""


def test_creators_are_the_named_data_centers_once_each():
    umm = {
        "DataCenters": [
            {"ShortName": " NASA/JPL/PODAAC "},
            {"LongName": "no short name"},
            {"ShortName": ""},
            {"ShortName": "NASA/JPL/PODAAC"},
            {"ShortName": "UK/MOD/MET"},
        ]
    }
    assert nasacmr._creators(umm) == [Creator(name="NASA/JPL/PODAAC"), Creator(name="UK/MOD/MET")]


def test_subjects_take_the_most_specific_present_leaf_once():
    umm = {
        "ScienceKeywords": [
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": "SEA ICE"},
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": None},
            {"Category": "EARTH SCIENCE", "Topic": None, "Term": None},
            {"Category": None, "Topic": None, "Term": None},
            {"Category": "EARTH SCIENCE", "Topic": "OCEANS", "Term": "SEA ICE"},
            {"Category": "X", "Topic": "OCEANS", "Term": ""},
        ]
    }
    assert nasacmr._subjects(umm) == ["SEA ICE", "OCEANS", "EARTH SCIENCE"]


@pytest.mark.parametrize(
    ("constraints", "expected"),
    [
        ({"LicenseURL": {"Linkage": "https://creativecommons.org/licenses/by/4.0/"}}, "CC-BY-4.0"),
        (
            {"LicenseText": "Licensed under https://creativecommons.org/licenses/by-sa/4.0/"},
            "CC-BY-SA-4.0",
        ),
        (
            {"Description": "conforms to (http://creativecommons.org/licenses/by/3.0/)."},
            "CC-BY-3.0",
        ),
        (  # the LicenseURL is read before the prose
            {
                "LicenseURL": {"Linkage": "https://creativecommons.org/publicdomain/zero/1.0/"},
                "Description": "https://creativecommons.org/licenses/by/4.0/",
            },
            "CC0-1.0",
        ),
        (  # a non-string leaf is skipped and the next one read
            {
                "LicenseURL": {"Linkage": ["https://creativecommons.org/licenses/by/4.0/"]},
                "Description": 7,
                "LicenseText": "see https://creativecommons.org/licenses/by-nc/4.0/",
            },
            "CC-BY-NC-4.0",
        ),
        (  # a NASA policy page is not a licence
            {
                "LicenseURL": {
                    "Linkage": "https://science.nasa.gov/earth-science/earth-science-data/"
                    "data-information-policy"
                }
            },
            None,
        ),
        ({"LicenseURL": "https://creativecommons.org/licenses/by/4.0/"}, None),
        ({"Description": "CC BY 4.0, no URL"}, None),
        ("https://creativecommons.org/licenses/by/4.0/", None),
    ],
)
def test_licence_is_read_only_from_an_embedded_cc_url(constraints, expected):
    lic, access = nasacmr._license_and_access({"UseConstraints": constraints})
    assert (lic, access) == (expected, "open" if expected else None)


@pytest.mark.parametrize(
    ("dates", "year"),
    [
        ([{"Type": "UPDATE", "Date": "2025-01-01"}, {"Type": "Production", "Date": "2018"}], 2018),
        (
            [{"Type": "REVIEW", "Date": "2025-01-01"}, {"Type": "create", "Date": "2017-03-01"}],
            2017,
        ),
        ([{"Type": None, "Date": "2024-01-01"}, {"Type": "CREATE", "Date": "2016-01-01"}], 2016),
        ([{"Type": "CREATE", "Date": "unknown"}, {"Type": "UPDATE", "Date": "2021-05-05"}], 2021),
        ([{"Type": None, "Date": "2015-01-01"}], 2015),
        ([{"Type": "CREATE", "Date": None}], None),
        ([], None),
    ],
)
def test_year_prefers_a_creation_date(dates, year):
    assert nasacmr._pub_year({"DataDates": dates}) == year


def test_the_data_access_link_is_the_first_get_data_url():
    umm = {
        "RelatedUrls": [
            {"Type": "VIEW RELATED INFORMATION", "URL": "https://docs/x"},
            {"Type": "GET DATA"},
            {"Type": "GET DATA", "URL": ""},
            {"Type": "GET DATA", "URL": "https://search.earthdata.nasa.gov/a"},
            {"Type": "GET DATA", "URL": "https://search.earthdata.nasa.gov/b"},
        ]
    }
    assert nasacmr._links(umm) == [
        Link(rel="data_access", target_id="https://search.earthdata.nasa.gov/a")
    ]
    assert nasacmr._links({"RelatedUrls": [{"Type": None, "URL": "https://x"}]}) == []
    assert nasacmr._links({}) == []
