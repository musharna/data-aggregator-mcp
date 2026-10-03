"""What the OpenAIRE adapter sends and how it reads what comes back, pinned exactly.

Shapes from the live Graph API (2026-10-02): see ``test_openaire_answers.py``.
"""

from __future__ import annotations

import copy
from urllib.parse import unquote

import httpx
import pytest

from data_aggregator_mcp import _http, fulltext, openaire
from data_aggregator_mcp.models import FileEntry
from tests.test_openaire_answers import _FULL, _HEADER


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)


def _recording(sent: list[httpx.Request], body: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _hits(n: int) -> list[dict]:
    return [{**_FULL, "id": f"oa::{i}"} for i in range(n)]


@pytest.fixture
def no_enrichment(monkeypatch):
    async def _no_links(client, doi):
        return [], None

    async def _no_ids(client, doi):
        return {}, None

    async def _no_fulltext(client, pmcid=None, doi=None):
        return fulltext.FullText()

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_links)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _no_ids)
    monkeypatch.setattr("data_aggregator_mcp.fulltext.find", _no_fulltext)


@pytest.mark.asyncio
async def test_each_search_request_is_exactly_what_openaire_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, {"header": _HEADER, "results": _hits(50)}) as c:
        first = await openaire.search(c, "soil moisture")
        capped = await openaire.search(c, "soil moisture", size=80)
        paged = await openaire.search(c, "soil moisture", size=10, offset=15)
    base = "https://api.openaire.eu/graph/v1/researchProducts"
    assert [(r.method, str(r.url)) for r in sent] == [
        ("GET", f"{base}?search=soil+moisture&type=publication&pageSize=10"),
        ("GET", f"{base}?search=soil+moisture&type=publication&pageSize=50"),
        ("GET", f"{base}?search=soil+moisture&type=publication&pageSize=10&page=2"),
    ]
    # The total is the upstream's, not the page's; a page starts at offset % size.
    assert first[0] == 221 and [r.id for r in first[1]][:2] == ["openaire:oa::0", "openaire:oa::1"]
    assert len(capped[1]) == 50
    assert [r.id for r in paged[1]][:2] == ["openaire:oa::5", "openaire:oa::6"]


@pytest.mark.asyncio
async def test_a_resolve_request_keeps_the_id_in_one_path_segment(no_enrichment):
    sent: list[httpx.Request] = []
    oid = "doi_dedup___::a/b?c#d"
    async with _recording(sent, {**_FULL, "id": oid}) as c:
        r = await openaire.resolve(c, f"openaire:{oid}")
    (req,) = sent
    assert req.method == "GET"
    assert req.url.raw_path == (b"/graph/v1/researchProducts/doi_dedup___%3A%3Aa%2Fb%3Fc%23d")
    assert unquote(req.url.raw_path.decode().rsplit("/", 1)[-1]) == oid
    assert r.id == f"openaire:{oid}"


@pytest.mark.parametrize(
    ("pids", "instances", "doi"),
    [
        # A PubMed id listed before the DOI is not the DOI.
        ([{"scheme": "pmid", "value": "1"}, {"scheme": "doi", "value": "10.1/a"}], [], "10.1/a"),
        ([{"scheme": "DOI", "value": "10.1/b"}], [], "10.1/b"),
        ([{"scheme": "doi", "value": ""}, {"scheme": "doi", "value": "10.1/c"}], [], "10.1/c"),
        (
            [{"scheme": None, "value": "x"}],
            [{"pids": [{"scheme": "doi", "value": "10.1/d"}]}],
            "10.1/d",
        ),
        (
            [{"scheme": "pmid", "value": "1"}],
            [{"pids": None}, {"pids": [{"scheme": "pmid"}]}],
            None,
        ),
    ],
)
def test_the_doi_is_the_first_pid_whose_scheme_is_doi(pids, instances, doi):
    assert openaire._normalize_openaire({**_FULL, "pids": pids, "instances": instances}).doi == doi


def test_a_missing_title_reads_as_empty_and_the_first_written_licence_wins():
    rec = copy.deepcopy(_FULL)
    rec["mainTitle"] = None
    rec["instances"] = [{"license": None}, {"license": "  "}, {"license": " CC BY "}]
    r = openaire._normalize_openaire(rec)
    assert (r.title, r.license) == ("", "CC BY")
    # Control: no licence on any instance is no licence.
    rec["instances"] = [{"license": None}, {"license": ""}]
    assert openaire._normalize_openaire(rec).license is None


@pytest.mark.asyncio
async def test_openaire_rights_stay_primary_over_the_full_text_record(monkeypatch):
    """EuropePMC fills access and licence only where OpenAIRE gave none."""

    async def _no_links(client, doi):
        return [], None

    async def _no_ids(client, doi):
        return {}, None

    async def _open_copy(client, pmcid=None, doi=None):
        file = FileEntry(name="fulltext.xml", url="https://x/f.xml", source="europepmc")
        return fulltext.FullText(file=file, access="open", license="cc by")

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", _no_links)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", _no_ids)
    monkeypatch.setattr("data_aggregator_mcp.fulltext.find", _open_copy)
    sent: list[httpx.Request] = []
    async with _recording(sent, _FULL) as c:
        kept = await openaire.resolve(c, f"openaire:{_FULL['id']}")
    assert (kept.access, kept.license) == ("closed", "Springer TDM")
    assert [f.source for f in kept.files] == ["europepmc"]
    bare = {k: v for k, v in _FULL.items() if k not in ("bestAccessRight", "instances")}
    async with _recording(sent, bare) as c:
        filled = await openaire.resolve(c, f"openaire:{_FULL['id']}")
    assert (filled.access, filled.license) == ("open", "cc by")
