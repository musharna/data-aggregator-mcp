"""What EBI OLS4 answers the ontology lookups' exact-name search, and how it is read.

The fixtures are live answers captured 2026-10-02 from
``GET /ols4/api/search?q=<name>&ontology=<o>&exact=true&queryFields=label,synonym
&fieldList=obo_id,label,synonym,is_defining_ontology,is_obsolete&rows=10``, with
``facet_counts`` (which nothing reads) dropped.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _http, anatomy, chemistry
from data_aggregator_mcp.errors import UpstreamUnavailableError

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_FIXTURES = Path(__file__).parent / "fixtures"


def _answer(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"ols_search_{name}.json").read_text(encoding="utf-8"))


LIVER = _answer("liver")  # UBERON, one label match
SKIN = _answer("skin")  # UBERON, four synonym matches, no label match
ASPIRIN = _answer("aspirin")  # ChEBI, one synonym match ("Aspirin")
EMPTY = _answer("empty")  # ChEBI, no match


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    async def _no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_wait)
    anatomy._CACHE.clear()
    chemistry._CACHE.clear()
    yield
    anatomy._CACHE.clear()
    chemistry._CACHE.clear()


def _serving(sent: list[httpx.Request], *bodies: Any) -> httpx.AsyncClient:
    """Answer the n-th request with ``bodies[n]`` (the last one repeats)."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=bodies[min(len(sent), len(bodies)) - 1])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- real answers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_answers_are_read_whole():
    sent: list[httpx.Request] = []
    async with _serving(sent, LIVER) as c:
        liver = await anatomy.resolve_uberon(c, "liver")
    async with _serving(sent, SKIN) as c:
        skin = await anatomy.resolve_uberon(c, "skin")
    async with _serving(sent, ASPIRIN) as c:
        aspirin = await chemistry.resolve_chebi(c, "aspirin")
    async with _serving(sent, EMPTY) as c:
        nothing = await chemistry.resolve_chebi(c, "zzznotachemical")
    assert liver == anatomy.UberonInfo("UBERON:0002107", "liver", ("iecur", "jecur"))
    # No label match: the first doc carrying "skin" as a synonym, in OLS's order.
    assert skin == anatomy.UberonInfo(
        "UBERON:0000014",
        "zone of skin",
        ("portion of skin", "region of skin", "skin region", "skin zone", "skin"),
    )
    assert aspirin is not None
    assert (aspirin.chebi_id, aspirin.canonical) == ("CHEBI:15365", "acetylsalicylic acid")
    # Case-duplicates collapse to their first spelling; capped at 12.
    assert aspirin.synonyms == (
        "2-(ACETYLOXY)BENZOIC ACID",
        "2-Acetoxybenzenecarboxylic acid",
        "2-acetoxybenzoic acid",
        "ASA",
        "Acetylsalicylate",
        "Acetylsalicylsaeure",
        "Aspirin",
        "Azetylsalizylsaeure",
        "Easprin",
        "O-acetylsalicylic acid",
        "acide 2-(acetyloxy)benzoique",
        "acide acetylsalicylique",
    )
    assert nothing is None
    assert len(sent) == 4


# --- malformed answers ------------------------------------------------------------

_DOC = LIVER["response"]["docs"][0]


def _with_doc(**fields: Any) -> dict[str, Any]:
    body = copy.deepcopy(LIVER)
    body["response"]["docs"][0].update(fields)
    return body


def _without(field: str) -> dict[str, Any]:
    body = copy.deepcopy(LIVER)
    del body["response"]["docs"][0][field]
    return body


_BROKEN = {
    "empty object": {},
    "error envelope": {"status": 500, "message": "Internal Server Error"},
    "response null": {"response": None},
    "response a list": {"response": []},
    "response without docs": {"response": {"numFound": 1, "start": 0}},
    "docs null": {"response": {"docs": None, "numFound": 0}},
    "docs an object": {"response": {"docs": {}, "numFound": 0}},
    "doc a string": {"response": {"docs": ["UBERON:0002107"], "numFound": 1}},
    "doc without obo_id": _without("obo_id"),
    "doc without label": _without("label"),
    "obo_id a number": _with_doc(obo_id=2107),
    "label null": _with_doc(label=None),
    "label a list": _with_doc(label=["liver"]),
    "synonym a number": _with_doc(synonym=5),
    "synonym an object": _with_doc(synonym={"0": "iecur"}),
    "synonym holding a number": _with_doc(synonym=["iecur", 5]),
    "is_defining_ontology a string": _with_doc(is_defining_ontology="true"),
    "is_defining_ontology a number": _with_doc(is_defining_ontology=1),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _BROKEN.values(), ids=_BROKEN.keys())
async def test_a_malformed_answer_is_an_outage_and_is_not_cached_as_no_match(body):
    sent: list[httpx.Request] = []
    async with _serving(sent, body, body, LIVER) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] EBI OLS \(UBERON\) returned an unparseable "
            r"200 body after 2 tries: UpstreamEnvelopeError\(.no OLS search docs in ",
        ):
            await anatomy.resolve_uberon(c, "liver")
        # Positive control, and the failure was not cached: the real answer is read.
        info = await anatomy.resolve_uberon(c, "liver")
    assert info is not None and info.uberon_id == "UBERON:0002107"
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_the_refusal_quotes_what_ols_sent():
    sent: list[httpx.Request] = []
    async with _serving(sent, {"response": None}) as c:
        with pytest.raises(UpstreamUnavailableError) as raised:
            await chemistry.resolve_chebi(c, "caffeine")
    assert str(raised.value) == (
        "[UpstreamUnavailableError] EBI OLS (ChEBI) returned an unparseable 200 body after 2 "
        "tries: UpstreamEnvelopeError(\"no OLS search docs in {'response': None}\")"
    )


_OPTIONAL = {
    "no synonym": (_without("synonym"), ()),
    "synonym one string": (_with_doc(synonym="iecur"), ("iecur",)),
    "synonym empty": (_with_doc(synonym=[]), ()),
    "no is_defining_ontology": (_without("is_defining_ontology"), ("iecur", "jecur")),
    "not defining": (_with_doc(is_defining_ontology=False), ("iecur", "jecur")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "synonyms"), _OPTIONAL.values(), ids=_OPTIONAL.keys())
async def test_a_doc_missing_an_optional_field_is_read(body, synonyms):
    sent: list[httpx.Request] = []
    async with _serving(sent, body) as c:
        info = await anatomy.resolve_uberon(c, "liver")
    assert info == anatomy.UberonInfo("UBERON:0002107", "liver", synonyms)
    assert len(sent) == 1


_JSON_VALUES = [None, True, 0, 1.5, "", "x", [], ["x"], [1], {}, {"x": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_DOC) + ["is_obsolete"])
async def test_no_wrong_typed_field_escapes_as_a_bare_error(field):
    # Every field of a real doc, with every JSON type: refused as an outage, or read.
    for value in _JSON_VALUES:
        anatomy._CACHE.clear()
        sent: list[httpx.Request] = []
        async with _serving(sent, _with_doc(**{field: value})) as c:
            try:
                info = await anatomy.resolve_uberon(c, "liver")
            except UpstreamUnavailableError:
                continue
        assert info is None or info.uberon_id == "UBERON:0002107", (field, value)


# --- live -------------------------------------------------------------------------

# Names whose exact match OLS ranked past the old 10-row relevance window
# (2026-10-02: aspirin 18th, skin 22nd, bone 68th), next to names it ranked first.
_LIVE_NAMES = [
    (chemistry.resolve_chebi, "aspirin", "CHEBI:15365"),
    (chemistry.resolve_chebi, "caffeine", "CHEBI:27732"),
    (anatomy.resolve_uberon, "skin", "UBERON:0000014"),
    (anatomy.resolve_uberon, "bone", "UBERON:0001474"),
    (anatomy.resolve_uberon, " liver ", "UBERON:0002107"),
]


@live_only
@pytest.mark.asyncio
async def test_live_a_name_ranked_low_by_relevance_is_still_found():
    async with httpx.AsyncClient() as c:
        found = [await resolve(c, name) for resolve, name, _ in _LIVE_NAMES]
        missing = await chemistry.resolve_chebi(c, "zzznotachemical")
    assert [getattr(i, "chebi_id", None) or getattr(i, "uberon_id", None) for i in found] == [
        want for _, _, want in _LIVE_NAMES
    ]
    assert missing is None


@live_only
@pytest.mark.asyncio
async def test_live_answers_pass_the_check():
    # The check must refuse none of what OLS really sends: a wide (non-exact) search
    # returns hundreds of docs per ontology.
    from data_aggregator_mcp import _ols

    seen = 0
    async with httpx.AsyncClient() as c:
        for ontology, q in (("uberon", "part"), ("chebi", "acid")):
            body = await _http.request_json(
                c,
                "GET",
                _ols.SEARCH,
                service="EBI OLS (live check)",
                params={
                    "q": q,
                    "ontology": ontology,
                    "fieldList": _ols._QUERY["fieldList"],
                    "rows": "500",
                },
                expect=dict,
            )
            _ols._check_search(body)
            seen += len(body["response"]["docs"])
    assert seen == 1000
