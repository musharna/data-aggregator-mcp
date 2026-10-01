"""What the full-text, id-conversion and Scholix lookups send, keep and report, pinned
(#88 burn-down of `fulltext`, `idconv`, `scholix`).

130 mutants survived the fix: every service name, log line and failure reason could
change unseen, as could the rights a lookup keeps when it finds no file, Unpaywall's
404, how ``find`` merges two legs, and which DOIs reach the doi.org batch. Each test
drives the code against a server that records the requests it gets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from data_aggregator_mcp import _http, fulltext, idconv, scholix
from data_aggregator_mcp.models import FileEntry

_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(*_a: object) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)


@pytest.fixture(autouse=True)
def no_emails(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NCBI_EMAIL", "UNPAYWALL_EMAIL"):
        monkeypatch.delenv(name, raising=False)


class _Server:
    """Answers by host; records every request."""

    def __init__(self, **by_host: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.by_host = by_host
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.by_host[request.url.host.replace(".", "_")]
        return answer(request) if callable(answer) else answer

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _hits(*results: dict) -> httpx.Response:
    return httpx.Response(200, json={"resultList": {"result": list(results)}})


def _warnings(caplog: pytest.LogCaptureFixture, module: object) -> list[str]:
    name = module.__name__
    return [r.getMessage() for r in caplog.records if r.name == name]


# --- EuropePMC ----------------------------------------------------------------------


async def test_europepmc_strips_quotes_from_a_doi_and_sends_one_core_json_result():
    server = _Server(www_ebi_ac_uk=_hits())
    async with server.client() as c:
        assert await fulltext._europepmc(c, None, '10.9/a"b') == fulltext.FullText()
    (sent,) = server.requests
    assert sent.method == "GET"
    assert str(sent.url).startswith(f"{_EPMC}/search?")
    assert dict(sent.url.params) == {
        "query": 'DOI:"10.9/ab"',
        "format": "json",
        "resultType": "core",
        "pageSize": "1",
    }


async def test_a_malformed_pmcid_without_a_doi_sends_nothing_and_says_why(caplog):
    server = _Server()
    with caplog.at_level(logging.WARNING, logger=fulltext.__name__):
        async with server.client() as c:
            assert await fulltext._europepmc(c, "PMC1 OR *", None) == fulltext.FullText()
            assert await fulltext._europepmc(c, None, None) == fulltext.FullText()
    assert server.requests == []
    assert _warnings(caplog, fulltext) == [
        "ignoring malformed pmcid 'PMC1 OR *' (expected PMC<digits>)"
    ]


async def test_a_failed_europepmc_lookup_is_named_and_logged_by_the_id_asked(caplog):
    server = _Server(www_ebi_ac_uk=httpx.Response(503))
    with caplog.at_level(logging.WARNING, logger=fulltext.__name__):
        async with server.client() as c:
            ft = await fulltext._europepmc(c, "PMC1", "10.9/x")
    cause = "EuropePMC search exhausted 3 retries (last HTTP 503)"
    assert ft == fulltext.FullText(
        error=f"EuropePMC lookup failed: UpstreamUnavailableError: [UpstreamUnavailableError] {cause}"
    )
    assert _warnings(caplog, fulltext) == [
        f"EuropePMC lookup failed for 'PMC1': UpstreamUnavailableError('{cause}')"
    ]


@pytest.mark.parametrize(
    "result",
    [
        {"inEPMC": "N", "isOpenAccess": "y", "license": "cc by"},
        {"inEPMC": "Y", "isOpenAccess": "Y", "license": "cc by"},  # in EPMC, no pmcid known
    ],
)
async def test_europepmc_keeps_the_rights_when_it_has_no_file(result):
    async with _Server(www_ebi_ac_uk=_hits(result)).client() as c:
        ft = await fulltext._europepmc(c, None, "10.9/x")
    assert ft == fulltext.FullText(access="open", license="cc by")


async def test_europepmc_reads_closed_access_as_no_access():
    async with _Server(www_ebi_ac_uk=_hits({"inEPMC": "N", "isOpenAccess": "N"})).client() as c:
        assert await fulltext._europepmc(c, None, "10.9/x") == fulltext.FullText()


@pytest.mark.parametrize(
    ("pmcid", "doi", "result", "named"),
    [
        (None, "10.9/x", {"inEPMC": "Y", "pmcid": "PMC2"}, "PMC2"),  # found by DOI
        ("PMC1", None, {"inEPMC": "Y"}, "PMC1"),  # the hit omits the PMCID we asked for
        ("PMC1", None, {"inEPMC": "Y", "pmcid": "PMC3"}, "PMC3"),  # EuropePMC's own wins
    ],
)
async def test_the_xml_file_is_named_by_the_pmcid_europepmc_holds(pmcid, doi, result, named):
    async with _Server(www_ebi_ac_uk=_hits(result)).client() as c:
        ft = await fulltext._europepmc(c, pmcid, doi)
    assert ft.file == FileEntry(
        name=f"{named}.xml",
        mime="application/xml",
        url=f"{_EPMC}/{named}/fullTextXML",
        source="europepmc",
    )


# --- Unpaywall ----------------------------------------------------------------------


async def test_without_an_email_unpaywall_is_skipped_and_says_so(caplog):
    server = _Server()
    with caplog.at_level(logging.WARNING, logger=fulltext.__name__):
        async with server.client() as c:
            assert await fulltext._unpaywall(c, "10.9/x") == fulltext.FullText()
    assert server.requests == []
    assert _warnings(caplog, fulltext) == [
        "full text: UNPAYWALL_EMAIL unset; skipping Unpaywall leg for '10.9/x'"
    ]


async def test_a_doi_unpaywall_does_not_know_is_no_copy_not_a_failure(monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    server = _Server(api_unpaywall_org=httpx.Response(404, json={"error": True}))
    async with server.client() as c:
        assert await fulltext._unpaywall(c, "10.9/x") == fulltext.FullText()
    (sent,) = server.requests
    assert sent.method == "GET"
    assert str(sent.url) == "https://api.unpaywall.org/v2/10.9/x?email=x%40y.z"


async def test_a_failed_unpaywall_lookup_is_named_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    with caplog.at_level(logging.WARNING, logger=fulltext.__name__):
        async with _Server(api_unpaywall_org=httpx.Response(503)).client() as c:
            ft = await fulltext._unpaywall(c, "10.9/x")
    cause = "Unpaywall exhausted 3 retries (last HTTP 503)"
    assert ft == fulltext.FullText(
        error=f"Unpaywall lookup failed: UpstreamUnavailableError: [UpstreamUnavailableError] {cause}"
    )
    assert _warnings(caplog, fulltext) == [
        f"Unpaywall lookup failed for '10.9/x': UpstreamUnavailableError('{cause}')"
    ]


@pytest.mark.parametrize(
    ("location", "file"),
    [
        ({"license": "cc-by", "url": "https://pub/landing"}, None),
        (
            {"license": "cc-by", "url_for_pdf": "https://r/x.pdf"},
            FileEntry(
                name="fulltext.pdf",
                mime="application/pdf",
                url="https://r/x.pdf",
                source="unpaywall",
            ),
        ),
    ],
)
async def test_an_open_unpaywall_record_keeps_its_rights_with_or_without_a_pdf(
    monkeypatch, location, file
):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    answer = httpx.Response(200, json={"is_oa": True, "best_oa_location": location})
    async with _Server(api_unpaywall_org=answer).client() as c:
        ft = await fulltext._unpaywall(c, "10.9/x")
    assert ft == fulltext.FullText(file=file, access="open", license="cc-by")


# --- find: merging the two legs -------------------------------------------------------


async def test_find_names_both_failed_legs(monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    server = _Server(www_ebi_ac_uk=httpx.Response(503), api_unpaywall_org=httpx.Response(503))
    async with server.client() as c:
        ft = await fulltext.find(c, doi="10.9/x")
    assert ft.file is None
    assert ft.error == (
        "EuropePMC lookup failed: UpstreamUnavailableError: [UpstreamUnavailableError] "
        "EuropePMC search exhausted 3 retries (last HTTP 503); "
        "Unpaywall lookup failed: UpstreamUnavailableError: [UpstreamUnavailableError] "
        "Unpaywall exhausted 3 retries (last HTTP 503)"
    )


@pytest.mark.parametrize(
    ("epmc", "rights"),
    [
        ({"inEPMC": "N", "isOpenAccess": "Y", "license": "cc by"}, ("open", "cc by")),
        ({"inEPMC": "N"}, ("open", "cc-by-nc")),  # only Unpaywall knew
    ],
)
async def test_find_without_a_file_prefers_europepmcs_rights_then_unpaywalls(
    monkeypatch, epmc, rights
):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "x@y.z")
    unpaywall = {"is_oa": True, "best_oa_location": {"license": "cc-by-nc"}}
    server = _Server(
        www_ebi_ac_uk=_hits(epmc), api_unpaywall_org=httpx.Response(200, json=unpaywall)
    )
    async with server.client() as c:
        ft = await fulltext.find(c, doi="10.9/x")
    assert ft == fulltext.FullText(access=rights[0], license=rights[1])


# --- idconv -------------------------------------------------------------------------


_IDCONV_HIT = {"records": [{"doi": "10.9/x", "pmid": 1, "pmcid": "PMC1"}]}


@pytest.mark.parametrize(
    ("env", "email"),
    [
        ({}, None),
        ({"UNPAYWALL_EMAIL": "u@y.z"}, "u@y.z"),
        ({"NCBI_EMAIL": "n@y.z", "UNPAYWALL_EMAIL": "u@y.z"}, "n@y.z"),
    ],
)
async def test_idconv_sends_the_ncbi_email_first_then_the_unpaywall_one(monkeypatch, env, email):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    server = _Server(www_ncbi_nlm_nih_gov=httpx.Response(200, json=_IDCONV_HIT))
    async with server.client() as c:
        assert await idconv.identifiers_for(c, "10.9/x") == (
            {"doi": "10.9/x", "pmid": "1", "pmcid": "PMC1"},
            None,
        )
    (sent,) = server.requests
    assert sent.method == "GET"
    assert str(sent.url).startswith(f"{idconv.BASE_URL}?")
    expected = {"ids": "10.9/x", "format": "json", "tool": "data-aggregator-mcp"}
    assert dict(sent.url.params) == expected | ({"email": email} if email else {})


async def test_a_failed_idconv_lookup_is_named_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=idconv.__name__):
        async with _Server(www_ncbi_nlm_nih_gov=httpx.Response(503)).client() as c:
            assert await idconv.identifiers_for(c, "10.9/x") == (
                {},
                "NCBI idconv lookup failed: UpstreamUnavailableError: [UpstreamUnavailableError] "
                "NCBI idconv exhausted 3 retries (last HTTP 503)",
            )
    assert _warnings(caplog, idconv) == [
        "idconv failed for '10.9/x': "
        "UpstreamUnavailableError('NCBI idconv exhausted 3 retries (last HTTP 503)')"
    ]


# --- Scholix ------------------------------------------------------------------------


def _link(target_type: str, doi: str | None, rel: object = "IsSupplementedBy") -> dict:
    identifiers = [{"ID": "50|abc", "IDScheme": "openaireIdentifier"}]
    if doi:
        identifiers.append({"ID": doi, "IDScheme": "DOI"})
    return {
        "RelationshipType": {"Name": rel} if rel is not None else None,
        "target": {"Identifier": identifiers, "Type": target_type},
    }


def _links(*links: dict) -> httpx.Response:
    return httpx.Response(200, json={"result": list(links)})


async def test_links_for_sends_the_source_pid_and_reads_every_data_target():
    """A target without a DOI, a citation and an unnamed relation do not end the read."""
    server = _Server(
        api_scholexplorer_openaire_eu=_links(
            _link("dataset", None),
            _link("publication", "10.1/paper"),
            _link("Dataset", "10.5/a", rel="Is Supplemented By"),
            _link("software", "10.5/b", rel=None),
            _link("dataset", "10.5/c", rel="References"),
        ),
        doi_org=httpx.Response(200, json=[]),
    )
    async with server.client() as c:
        links, error = await scholix.links_for(c, "10.9/x")
    assert [(link.rel, link.target_id) for link in links] == [
        ("is_supplemented_by", "10.5/a"),
        ("is_related_to", "10.5/b"),
        ("references", "10.5/c"),
    ]
    assert error is None
    scholix_request, ra_request = server.requests
    assert scholix_request.method == "GET"
    assert str(scholix_request.url) == f"{scholix.BASE_URL}?sourcePid=10.9%2Fx"
    assert ra_request.method == "GET"
    assert ra_request.url.raw_path == b"/ra/10.5/a,10.5/b,10.5/c"


async def test_a_doi_with_a_comma_stays_bare_and_out_of_the_agency_batch():
    server = _Server(
        api_scholexplorer_openaire_eu=_links(
            _link("dataset", "10.5/a,b"), _link("dataset", "10.5/c")
        ),
        doi_org=httpx.Response(200, json=[{"DOI": "10.5/C", "RA": "DataCite"}]),
    )
    async with server.client() as c:
        links, error = await scholix.links_for(c, "10.9/x")
    assert [link.target_id for link in links] == ["10.5/a,b", "datacite:10.5/c"]
    assert error is None
    assert server.requests[-1].url.raw_path == b"/ra/10.5/c"


async def test_a_failed_scholix_lookup_is_logged_with_the_pid(caplog):
    with caplog.at_level(logging.WARNING, logger=scholix.__name__):
        async with _Server(api_scholexplorer_openaire_eu=httpx.Response(503)).client() as c:
            await scholix.links_for(c, "10.9/x")
    assert _warnings(caplog, scholix) == [
        "ScholeXplorer lookup failed for '10.9/x': "
        "[UpstreamUnavailableError] ScholeXplorer exhausted 3 retries (last HTTP 503)"
    ]


async def test_a_failed_agency_lookup_leaves_the_links_bare_and_says_why(caplog):
    server = _Server(
        api_scholexplorer_openaire_eu=_links(_link("dataset", "10.5/a")),
        doi_org=httpx.Response(503),
    )
    with caplog.at_level(logging.WARNING, logger=scholix.__name__):
        async with server.client() as c:
            links, error = await scholix.links_for(c, "10.9/x")
    cause = (
        "[UpstreamUnavailableError] DOI registration-agency lookup exhausted 3 retries "
        "(last HTTP 503)"
    )
    assert [link.target_id for link in links] == ["10.5/a"]
    assert error == (
        f"doi.org registration-agency lookup failed (UpstreamUnavailableError: {cause}); "
        "1 link(s) left as bare DOIs, not checked for DataCite"
    )
    assert _warnings(caplog, scholix) == [
        f"doi.org RA lookup failed for 1 DOI(s); links stay bare: {cause}"
    ]
