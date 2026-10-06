"""Europe PMC text-mined accessions against verbatim answers (captured 2026-10-06), the
paper resolvers that add them, and live.

The round-2 studies behind Boothby 2017 (PRJNA369152, R3 S3) and the snow leopard
virome paper (PRJNA626440, R4 S36) have no PubMed link either way; Europe PMC names both
from the papers' text.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from data_aggregator_mcp import europepmc, openaire, router
from data_aggregator_mcp._cache import MISS
from data_aggregator_mcp.models import Link

_FIX = Path(__file__).parent / "fixtures"
# articleIds=MED:28306513 (Boothby 2017): a BioProject, five SRA runs, two SRA experiments.
_BOOTHBY = json.loads((_FIX / "europepmc_boothby2017.json").read_text())
# articleIds=MED:33195503: two RRIDs, a GenBank sequence and the BioProject.
_VIROME = json.loads((_FIX / "europepmc_snow_leopard_virome.json").read_text())
# articleIds=MED:20226016 (Mali 2010): GO terms only.
_GO_ONLY = json.loads((_FIX / "europepmc_go_terms_only.json").read_text())
LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"

_BOOTHBY_LINKS = [
    "bioproject:PRJNA369152",
    "sra:SRR5217912",
    "sra:SRR5217911",
    "sra:SRR5217848",
    "sra:SRR5217652",
    "sra:SRR5217467",
    "sra:SRX426237",
    "sra:SRX426240",
]


@pytest.fixture(autouse=True)
def _real(text_mining):
    """Every test here asks the real ``mined_links`` (conftest stubs it elsewhere)."""


@pytest.fixture
def no_backoff(monkeypatch):
    """Retry a malformed answer without waiting. Not for live tests: ``asyncio.sleep`` is
    process-wide, so it also takes the NCBI rate limiter's spacing away."""

    async def instant(_delay):
        return None

    monkeypatch.setattr(europepmc._http.asyncio, "sleep", instant)


def _client(sent: list[httpx.Request], body: object):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _mined(body: object, **ids):
    sent: list[httpx.Request] = []
    async with _client(sent, body) as c:
        return await europepmc.mined_links(c, **ids), sent


async def test_the_deposits_a_paper_names_become_references_links():
    (links, cut, error), sent = await _mined(_BOOTHBY, pmid="28306513")
    assert [dict(r.url.params) for r in sent] == [
        {"articleIds": "MED:28306513", "type": "Accession Numbers", "format": "JSON"}
    ]
    assert str(sent[0].url).startswith(europepmc.BASE_URL + "?")
    assert [link.target_id for link in links] == _BOOTHBY_LINKS
    assert {link.rel for link in links} == {"references"}
    assert (cut, error) == (None, None)


async def test_reagents_reference_records_and_single_sequences_are_not_data():
    (links, _, error), _ = await _mined(_VIROME, pmid="33195503")
    # RRID:SCR_018354, RRID:SCR_012983 (software) and MH070348 (one GenBank sequence)
    # are dropped; the BioProject named in the Data Availability section stays.
    assert [link.target_id for link in links] == ["bioproject:PRJNA626440"]
    (links, _, error), _ = await _mined(_GO_ONLY, pmid="20226016")
    assert (links, error) == ([], None)


async def test_a_paper_without_a_pmid_is_asked_for_by_pmcid_and_without_either_not_at_all():
    _, sent = await _mined(_VIROME, pmcid="pmc7536260")
    assert sent[0].url.params["articleIds"] == "PMC:PMC7536260"
    # Both known: the PMID is asked for.
    _, sent = await _mined(_VIROME, pmid="33195503", pmcid="PMC7536260")
    assert sent[0].url.params["articleIds"] == "MED:33195503"
    (links, cut, error), sent = await _mined(_VIROME)
    assert (links, cut, error, sent) == ([], None, None, [])


async def test_an_article_europe_pmc_does_not_know_names_nothing():
    (links, cut, error), sent = await _mined([], pmid="999999999999")
    assert (links, cut, error) == ([], None, None)
    assert len(sent) == 1


_ENVELOPE = 'UpstreamEnvelopeError("no list of annotated articles in '


@pytest.mark.parametrize(
    ("body", "why"),
    [
        pytest.param(
            {"resultList": {"result": [{"inEPMC": "N"}]}}, "expected list JSON", id="an-object"
        ),
        pytest.param([{"source": "MED"}], _ENVELOPE, id="no-annotations"),
        pytest.param([{"annotations": [{"tags": []}]}], _ENVELOPE, id="annotation-without-exact"),
        pytest.param(
            [{"annotations": [{"exact": "X", "tags": "X"}]}], _ENVELOPE, id="tags-not-a-list"
        ),
        pytest.param(
            [{"annotations": [{"exact": "X", "tags": [{"uri": "u"}]}]}], _ENVELOPE, id="tag-no-name"
        ),
        pytest.param(["MED:1"], _ENVELOPE, id="article-not-an-object"),
    ],
)
async def test_an_off_contract_answer_is_named_never_read_as_no_data(body, why, no_backoff, caplog):
    (links, cut, error), sent = await _mined(body, pmid="28306513")
    assert (links, cut) == ([], None)
    assert error.startswith(
        "Europe PMC annotations lookup failed (UpstreamUnavailableError: "
        "[UpstreamUnavailableError] Europe PMC annotations returned an unparseable 200 body"
    )
    assert why in error
    assert error.endswith("; data accessions named in the text unknown")
    assert len(sent) == 3  # retried, as any malformed 200
    [record] = [r for r in caplog.records if r.name == "data_aggregator_mcp.europepmc"]
    assert record.getMessage().startswith(
        "Europe PMC annotations lookup failed for MED:28306513: [UpstreamUnavailableError] "
    )
    # Positive control: the verbatim answer is read.
    (links, _, error), _ = await _mined(_BOOTHBY, pmid="28306513")
    assert len(links) == 8 and error is None


async def test_an_annotation_is_read_by_its_tag_not_the_words_around_it():
    """The miner's ``exact`` is the span of text; the tag names the accession."""
    body = [{"annotations": [{"exact": "PRJNA369152;", "tags": [{"name": "PRJNA369152"}]}]}]
    (links, _, _), _ = await _mined(body, pmid="1")
    assert [link.target_id for link in links] == ["bioproject:PRJNA369152"]


async def test_an_annotation_without_tags_is_read_by_its_text():
    body = [{"annotations": [{"exact": " gse12345 "}, {"exact": "GSE12345", "tags": []}]}]
    (links, _, _), _ = await _mined(body, pmid="1")
    assert [link.target_id for link in links] == ["geo:GSE12345"]


async def test_a_paper_naming_more_than_the_cap_is_cut_and_says_so():
    body = copy.deepcopy(_BOOTHBY)
    body[0]["annotations"] = [
        {"exact": f"GSM{n}", "tags": [{"name": f"GSM{n}", "uri": "u"}]} for n in range(150)
    ] + [{"exact": "GSM0", "tags": [{"name": "GSM0", "uri": "u"}]}]  # named twice, kept once
    (links, cut, error), _ = await _mined(body, pmid="1")
    assert [link.target_id for link in links] == [f"geo:GSM{n}" for n in range(100)]
    assert cut == "first 100 of 150 data accessions named in the text"
    assert error is None
    # At the cap: nothing is cut.
    body[0]["annotations"] = body[0]["annotations"][:100]
    (links, cut, _), _ = await _mined(body, pmid="1")
    assert (len(links), cut) == (100, None)


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("PRJNA369152", "bioproject:PRJNA369152"),
        ("PRJEB59868", "bioproject:PRJEB59868"),
        ("PRJDB2359", "bioproject:PRJDB2359"),
        ("prjna369152", "bioproject:PRJNA369152"),
        ("PRJCA022406", "ngdc:PRJCA022406"),
        ("GSE94295", "geo:GSE94295"),
        ("GSM5182729", "geo:GSM5182729"),
        ("GPL570", "geo:GPL570"),
        ("GDS507", "geo:GDS507"),
        ("SRP098563", "sra:SRP098563"),
        ("ERX123", "sra:ERX123"),
        ("DRR013911", "sra:DRR013911"),
        ("E-MTAB-10367", "biostudies:E-MTAB-10367"),
        ("GCST90319184", "gwas:GCST90319184"),
        ("PXD020726", "PXD020726"),
        ("MTBLS266", "MTBLS266"),
        ("SRS123", None),  # a sample
        ("SRA012345", None),  # a submission
        ("MH070348", None),
        ("RRID:SCR_018354", None),
        ("GO:0000786", None),
        ("P36925", None),
        ("PRJNA369152x", None),
        ("GSE", None),
        ("E-MTAB-", None),
    ],
)
def test_only_whole_data_accessions_are_routed(name, target):
    assert europepmc._target(name) == target


def test_a_mined_link_never_repeats_a_deposit_already_linked():
    elinked = [Link(rel="has_data", target_id="bioproject:PRJNA369152")]
    mined = [
        Link(rel="references", target_id="BIOPROJECT:prjna369152"),
        Link(rel="references", target_id="sra:SRR5217912"),
    ]
    assert europepmc.merge(elinked, mined) == [
        elinked[0],
        Link(rel="references", target_id="sra:SRR5217912"),
    ]
    assert europepmc.merge([], mined) == mined


# --- the resolvers --------------------------------------------------------------------

_EUT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_ANNOTATIONS = httpx.URL(
    europepmc.BASE_URL,
    params={"articleIds": "MED:28306513", "type": "Accession Numbers", "format": "JSON"},
)


def _pubmed_paper(httpx_mock: HTTPXMock) -> None:
    """PubMed's answers for a paper with no elink edges, no full text and no abstract."""
    doc = {
        "uid": "28306513",
        "title": "Tardigrades Use Intrinsically Disordered Proteins to Survive Desiccation.",
        "sortpubdate": "2017/03/16 00:00",
        "articleids": [{"idtype": "pubmed", "value": "28306513"}],
    }
    httpx_mock.add_response(
        url=f"{_EUT}/esummary.fcgi?db=pubmed&id=28306513&version=2.0&retmode=json",
        json={"result": {"uids": ["28306513"], "28306513": doc}},
    )
    for db in ("sra", "gds", "bioproject"):
        httpx_mock.add_response(
            url=f"{_EUT}/elink.fcgi?dbfrom=pubmed&db={db}&id=28306513&retmode=json",
            json={"linksets": [{}]},
        )
    httpx_mock.add_response(
        url=f"{_EUT}/efetch.fcgi?db=pubmed&id=28306513&retmode=xml",
        text="<PubmedArticleSet></PubmedArticleSet>",
    )


async def test_pubmed_resolve_adds_the_deposits_the_paper_names(httpx_mock, monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    _pubmed_paper(httpx_mock)
    httpx_mock.add_response(url=_ANNOTATIONS, json=_BOOTHBY)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "pubmed:28306513")
    assert [link.target_id for link in r.links] == _BOOTHBY_LINKS
    assert "links" not in r.errors and "links" not in r.truncated
    # A record every lookup answered for is cached (the control for the test below).
    assert router._RESOLVE_CACHE.get("pubmed:28306513") is not MISS


async def test_pubmed_resolve_names_a_failed_lookup_and_is_not_cached(
    httpx_mock, monkeypatch, no_backoff
):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    _pubmed_paper(httpx_mock)
    httpx_mock.add_response(url=_ANNOTATIONS, json={"error": "x"}, is_reusable=True)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "pubmed:28306513")
    assert r.links == []
    assert r.errors["links"].startswith("Europe PMC annotations lookup failed")
    # Not cached: the next resolve asks again.
    assert router._RESOLVE_CACHE.get("pubmed:28306513") is MISS


async def test_openaire_resolve_adds_them_after_scholix_without_repeating_one(
    httpx_mock, monkeypatch
):
    async def scholix_links(client, doi):
        return [Link(rel="is_supplemented_by", target_id="bioproject:PRJNA369152")], None

    async def ids(client, doi):
        return {"doi": doi, "pmid": "28306513"}, None

    async def no_fulltext(client, *, pmcid=None, doi=None):
        return openaire.fulltext.FullText(file=None, access=None, license=None, error=None)

    monkeypatch.setattr("data_aggregator_mcp.scholix.links_for", scholix_links)
    monkeypatch.setattr("data_aggregator_mcp.idconv.identifiers_for", ids)
    monkeypatch.setattr("data_aggregator_mcp.fulltext.find", no_fulltext)
    httpx_mock.add_response(
        url="https://api.openaire.eu/graph/v1/researchProducts/oai123",
        json={
            "id": "oai123",
            "mainTitle": "t",
            "type": "publication",
            "pids": [{"scheme": "doi", "value": "10.1016/j.molcel.2017.02.018"}],
        },
    )
    httpx_mock.add_response(url=_ANNOTATIONS, json=_BOOTHBY)
    async with httpx.AsyncClient() as c:
        r = await openaire.resolve(c, "openaire:oai123")
    assert [(link.rel, link.target_id) for link in r.links] == [
        ("is_supplemented_by", "bioproject:PRJNA369152"),
        *(("references", t) for t in _BOOTHBY_LINKS[1:]),
    ]
    assert "links" not in r.errors


# --- live ------------------------------------------------------------------------------

_live = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live
@pytest.mark.parametrize(
    ("paper", "deposit"),
    [
        ("pubmed:28306513", "bioproject:PRJNA369152"),  # R3 S3
        ("pubmed:33195503", "bioproject:PRJNA626440"),  # R4 S36
    ],
)
async def test_live_a_resolved_paper_names_the_benchmark_deposit(live_env, paper, deposit):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await router.resolve(c, paper)
    assert deposit in {link.target_id for link in r.links}, r.links
    assert "links" not in r.errors, r.errors
