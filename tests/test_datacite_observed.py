"""What the DataCite adapter sends and what it reads off a record, value by value
(#88 mutant burn-down). Rights entries follow the shapes DataCite serves: a licence
names itself by ``rightsIdentifier`` and/or ``rights``; an info:eu-repo access status
sits in ``rightsUri`` or, on some clients, in ``rights``."""

import httpx
import pytest

from data_aggregator_mcp import _http, datacite
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import Creator, Link, Metrics


def _record(doi: str = "10.1234/abc", client: str | None = "tdl.tdl", **attributes) -> dict:
    item: dict = {
        "id": doi,
        "type": "dois",
        "attributes": {"doi": doi, "titles": [{"title": "T"}], **attributes},
    }
    if client is not None:
        item["relationships"] = {"client": {"data": {"id": client, "type": "clients"}}}
    return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- requests -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_and_resolve_request_json_with_get():
    seen: list[httpx.Request] = []
    page = {"data": [_record()], "meta": {"total": 1}}
    async with _recording(httpx.Response(200, json=page), seen) as c:
        await datacite.search(c, "ocean")
    async with _recording(httpx.Response(200, json={"data": _record()}), seen) as c:
        await datacite.resolve(c, "datacite:10.1234/abc")
    assert [(r.method, r.url.path, r.headers["Accept"]) for r in seen] == [
        ("GET", "/dois", "application/json"),
        ("GET", "/dois/10.1234/abc", "application/json"),
    ]


@pytest.mark.parametrize(
    ("call", "service"),
    [
        (lambda c: datacite.search(c, "ocean"), "DataCite search"),
        (lambda c: datacite.resolve(c, "datacite:10.1234/abc"), "DataCite resolve"),
    ],
)
@pytest.mark.asyncio
async def test_an_outage_names_the_service_and_its_three_tries(call, service):
    seen: list[httpx.Request] = []
    async with _recording(httpx.Response(503), seen) as c:
        with pytest.raises(UpstreamUnavailableError) as exc:
            await call(c)
    assert str(exc.value) == (
        f"[UpstreamUnavailableError] {service} exhausted 3 retries (last HTTP 503)"
    )
    assert len(seen) == 3


# --- rights -> license / access -------------------------------------------------------


def _rights(*entries: dict):
    r = datacite._normalize(_record(rightsList=list(entries)))
    return r.license, r.access


def test_a_licence_named_only_by_its_rights_text_is_the_licence():
    assert _rights({"rights": "Creative Commons Attribution 4.0"}) == (
        "Creative Commons Attribution 4.0",
        None,
    )
    # The identifier wins over the name when both are given (control).
    assert _rights({"rights": "CC BY 4.0", "rightsIdentifier": "cc-by-4.0"}) == (
        "cc-by-4.0",
        "open",
    )


def test_an_entry_naming_no_licence_is_skipped_for_the_next():
    assert _rights({"rightsUri": "https://example.org/terms"}, {"rights": "Custom"}) == (
        "Custom",
        None,
    )


def test_an_access_status_given_in_the_rights_text_is_a_status_not_a_licence():
    status = {"rights": "info:eu-repo/semantics/embargoedAccess"}
    assert _rights(status, {"rightsIdentifier": "cc-by-4.0"}) == ("cc-by-4.0", "embargoed")


@pytest.mark.parametrize(
    ("entry", "access"),
    [
        ({"rightsUri": "https://creativecommons.org/publicdomain/zero/1.0/"}, "open"),
        ({"rightsUri": "HTTPS://CREATIVECOMMONS.ORG/LICENSES/BY/4.0/"}, "open"),
        ({"rightsUri": "http://www.example.org/publicdomain/mark"}, "open"),
        ({"rightsIdentifier": "CC-BY-4.0"}, "open"),
        ({"rightsUri": "http://paywall.example.com/creativecommons.org"}, None),
        ({"rightsIdentifier": "apache-2.0"}, None),
    ],
)
def test_access_is_open_only_for_an_open_content_licence(entry, access):
    assert datacite._normalize(_record(rightsList=[entry])).access == access


# --- creators, metrics, links, the rest -------------------------------------------------


def test_the_orcid_is_found_past_another_identifier_and_in_an_upper_case_url():
    isni = {"nameIdentifier": "0000-0001-2103-2683", "nameIdentifierScheme": "ISNI"}
    orcid = {"nameIdentifier": "0000-0002-1825-0097", "nameIdentifierScheme": "orcid"}
    url = {"nameIdentifier": "HTTPS://ORCID.ORG/0000-0002-1825-0097"}
    r = datacite._normalize(
        _record(
            creators=[
                {"name": "A", "nameIdentifiers": [isni, orcid]},
                {"name": "B", "nameIdentifiers": [url]},
                {"name": "C", "nameIdentifiers": [{"nameIdentifierScheme": "ORCID"}]},
            ]
        )
    )
    assert r.creators == [
        Creator(name="A", orcid="0000-0002-1825-0097"),
        Creator(name="B", orcid="0000-0002-1825-0097"),
        Creator(name="C"),
    ]


@pytest.mark.parametrize(
    ("counts", "metrics"),
    [
        ({"citationCount": 3}, Metrics(citations=3)),
        ({"viewCount": 4}, Metrics(views=4)),
        ({"downloadCount": 5}, Metrics(downloads=5)),
        ({}, None),
    ],
)
def test_metrics_keep_whichever_counts_are_listed(counts, metrics):
    assert datacite._normalize(_record(**counts)).metrics == metrics


def test_a_relation_without_both_ends_is_not_a_link():
    related = [
        {"relationType": "IsSupplementTo"},
        {"relatedIdentifier": "10.1/x"},
        {"relationType": "IsPartOf", "relatedIdentifier": "10.1/y"},
    ]
    r = datacite._normalize(_record(relatedIdentifiers=related))
    assert r.links == [Link(rel="is_part_of", target_id="10.1/y")]


def test_the_plain_fields_of_a_record():
    r = datacite._normalize(
        _record(
            publicationYear="2023",
            subjects=[{"subject": "ocean"}, {"subjectScheme": "x"}, {"subject": ""}],
            types={"resourceTypeGeneral": "Software"},
        )
    )
    assert (r.year, r.subjects, r.kind) == (2023, ["ocean"], "software")
    bare = datacite._normalize(_record(client=None, titles=None, publicationYear="n.d."))
    assert (bare.source, bare.title, bare.year, bare.kind) == ("", "", None, "other")


# --- resolve: hand-offs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("doi", "client"),
    [
        ("10.1234/abc", "cern.zenodo"),  # a Zenodo client, no zenodo.<n> in the DOI
        ("10.1234/zenodo.abc", "cern.zenodo"),  # not a record number
        ("10.1234/zenodo.7", "tdl.tdl"),  # zenodo.<n> in a DOI of another client
    ],
)
@pytest.mark.asyncio
async def test_only_a_zenodo_record_number_hands_the_record_to_zenodo(doi, client):
    seen: list[httpx.Request] = []
    body = {"data": _record(doi=doi, client=client)}
    async with _recording(httpx.Response(200, json=body), seen) as c:
        r = await datacite.resolve(c, f"datacite:{doi}")
    assert [req.url.host for req in seen] == ["api.datacite.org"]
    assert r.id == f"datacite:{doi}"
