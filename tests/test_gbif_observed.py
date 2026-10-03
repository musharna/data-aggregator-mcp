"""What the GBIF adapter sends and how it reads what comes back, pinned exactly.

Shapes from the live API (2026-10-02): see ``test_gbif_answers.py``.
"""

import httpx
import pytest

from data_aggregator_mcp import _http, gbif
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import Creator
from tests.test_gbif_answers import _HIT, _KEY, _NO_HITS, _RECORD, _page


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(sent: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _call(r: httpx.Request) -> tuple[str, str]:
    return r.method, str(r.url)


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_gbif_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=_page(_HIT))) as c:
        await gbif.search(c, "amphibian")
        await gbif.search(c, "frog toad", size=60, offset=50)
    async with _recording(sent, httpx.Response(200, json=_RECORD)) as c:
        await gbif.resolve(c, f"gbif:{_KEY}")
    assert [_call(s) for s in sent] == [
        ("GET", "https://api.gbif.org/v1/dataset/search?q=amphibian&limit=10&offset=0"),
        ("GET", "https://api.gbif.org/v1/dataset/search?q=frog+toad&limit=50&offset=50"),
        ("GET", f"https://api.gbif.org/v1/dataset/{_KEY}"),
    ]


@pytest.mark.asyncio
async def test_a_failing_call_names_its_gbif_endpoint_after_three_tries():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GBIF dataset search exhausted 3 retries "
            r"\(last HTTP 503\)$",
        ):
            await gbif.search(c, "amphibian")
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] GBIF dataset exhausted 3 retries "
            r"\(last HTTP 503\)$",
        ):
            await gbif.resolve(c, f"gbif:{_KEY}")
    assert len(sent) == 6
    # Positive control: the same client reads a healthy answer.
    async with _recording(sent, httpx.Response(200, json=_NO_HITS)) as c:
        assert await gbif.search(c, "amphibian") == (0, [])


@pytest.mark.asyncio
async def test_an_unknown_key_is_not_found_with_gbifs_message():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(404, text="Entity not found for uri: /")) as c:
        with pytest.raises(
            NotFoundError, match=rf"^\[NotFoundError\] GBIF has no dataset '{_KEY}'$"
        ):
            await gbif.resolve(c, f"gbif:{_KEY}")
    assert len(sent) == 1  # a 404 is an answer, not retried


def _creators(*contacts: dict, org: str | None = None) -> list[Creator]:
    return gbif._normalize(
        {"key": _KEY, "contacts": list(contacts), "publishingOrganizationTitle": org}
    ).creators


def test_creators_read_every_author_contact_in_order_once():
    admin = {"type": "ADMINISTRATIVE_POINT_OF_CONTACT", "firstName": "A", "lastName": "Admin"}
    assert _creators(
        admin,  # a non-author first does not end the walk
        {"type": "originator", "firstName": " Olivier ", "lastName": "Pauwels"},
        {"type": "PRINCIPAL_INVESTIGATOR", "lastName": "Kok"},
        {"type": "AUTHOR", "firstName": "Gaston"},
        {"type": "METADATA_AUTHOR", "organization": " RBINS "},
        {"type": "AUTHOR", "firstName": "Olivier", "lastName": "Pauwels"},
        {"type": None, "firstName": "No", "lastName": "Type"},
        org="Publisher",
    ) == [
        Creator(name="Olivier Pauwels"),
        Creator(name="Kok"),
        Creator(name="Gaston"),
        Creator(name="RBINS"),
    ]


def test_creators_fall_back_to_the_publisher_only_when_no_author_is_named():
    nameless = {"type": "AUTHOR", "firstName": None, "lastName": " ", "organization": None}
    assert _creators(nameless, org=" Publisher ") == [Creator(name="Publisher")]
    assert _creators(nameless) == []
    assert _creators({"type": "AUTHOR", "lastName": "Kok"}, org="Publisher") == [
        Creator(name="Kok")
    ]


def test_subjects_merge_flat_and_collected_keywords_once_in_order():
    doc = {
        "key": _KEY,
        "keywords": ["Occurrence", "", "Specimen", "Occurrence"],
        "keywordCollections": [
            {"keywords": ["Specimen", "Amphibia", None]},
            {"thesaurus": "N/A"},
            {"keywords": ["Amphibia", "RBINS"]},
        ],
    }
    assert gbif._normalize(doc).subjects == ["Occurrence", "Specimen", "Amphibia", "RBINS"]


@pytest.mark.parametrize(
    ("dates", "year"),
    [
        ({"pubDate": "2025-03-13", "publicationDate": "2015-04-08", "created": "2001"}, 2025),
        ({"publicationDate": "2015-04-08", "created": "2001-01-01"}, 2015),
        ({"created": "2024-10-09T13:18:17.549+00:00"}, 2024),
        ({}, None),
    ],
)
def test_year_is_the_first_of_pubdate_publicationdate_created(dates, year):
    assert gbif._normalize({"key": _KEY, **dates}).year == year


def test_a_record_with_only_a_key_reads_as_an_empty_dataset():
    r = gbif._normalize({"key": _KEY})
    assert (r.id, r.source, r.kind, r.title, r.creators, r.subjects, r.files) == (
        f"gbif:{_KEY}",
        "gbif",
        "dataset",
        "",
        [],
        [],
        [],
    )
    assert (r.year, r.description, r.doi, r.license, r.access, r.last_updated) == (None,) * 6


def test_archive_files_are_every_dwca_endpoint_with_a_url():
    doc = {
        "key": _KEY,
        "endpoints": [
            {"type": "EML", "url": "https://ipt.example/eml.do?r=a"},
            {"type": "DWC_ARCHIVE", "url": None},
            {"url": "https://ipt.example/untyped"},
            {"type": "dwc_archive", "url": "https://ipt.example/archive.do?r=a"},
            {"type": "DWC_ARCHIVE", "url": "https://mirror.example/archive.do?r=a"},
        ],
    }
    files = gbif._archive_files(doc)
    assert [(f.name, f.url, f.mime, f.source, f.checksum) for f in files] == [
        (f"{_KEY}.dwca.zip", "https://ipt.example/archive.do?r=a", "application/zip", "gbif", None),
        (
            f"{_KEY}.dwca.zip",
            "https://mirror.example/archive.do?r=a",
            "application/zip",
            "gbif",
            None,
        ),
    ]
    assert gbif._archive_files({"key": _KEY}) == []  # a metadata-only dataset
