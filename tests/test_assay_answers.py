"""What EBI OLS4 answers the ``assay`` lookup's exact-name search in EDAM, and how it is read.

The fixtures are live answers captured 2026-10-02 from
``GET /ols4/api/search?q=<name>&ontology=edam&exact=true&queryFields=label,synonym
&fieldList=obo_id,label,synonym,is_defining_ontology,is_obsolete&rows=10``, with
``facet_counts`` (which nothing reads) dropped.
"""

from __future__ import annotations

import copy
import os
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _http, assay
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test__ols_answers import EMPTY, _answer, _serving

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

CHIPSEQ = _answer("edam_chipseq")  # one label match
GENES = _answer("edam_genes")  # one synonym match ("Genes" of "Genetics")
PROTEIN_STRUCTURE = _answer("edam_protein_structure")  # an EDAM data_ term first, then the topic


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    async def _no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_wait)
    assay._CACHE.clear()
    yield
    assay._CACHE.clear()


# --- real answers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_answers_are_read_whole():
    sent: list[httpx.Request] = []
    found = []
    for body, name in (
        (CHIPSEQ, "ChIP-seq"),
        (GENES, "genes"),
        (PROTEIN_STRUCTURE, "Protein structure"),
        (EMPTY, "zzznotanassay"),
    ):
        async with _serving(sent, body) as c:
            found.append(await assay.resolve_edam(c, name))
    assert found == [
        assay.EdamInfo(
            "EDAM:topic_3169",
            "ChIP-seq",
            ("ChIP-exo", "ChIP-sequencing", "Chip Seq", "Chip sequencing", "Chip-sequencing"),
        ),
        assay.EdamInfo("EDAM:topic_3053", "Genetics", ("Genes", "Heredity")),
        # The data_ term whose label is the name is passed over for the topic.
        assay.EdamInfo(
            "EDAM:topic_2814",
            "Protein structure analysis",
            ("Protein structure", "Protein tertiary structure"),
        ),
        None,
    ]
    assert len(sent) == 4


# --- malformed answers ------------------------------------------------------------

_DOC = CHIPSEQ["response"]["docs"][0]


def _with_doc(**fields: Any) -> dict[str, Any]:
    body = copy.deepcopy(CHIPSEQ)
    body["response"]["docs"][0].update(fields)
    return body


def _without(field: str) -> dict[str, Any]:
    body = copy.deepcopy(CHIPSEQ)
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
    "doc a string": {"response": {"docs": ["EDAM:topic_3169"], "numFound": 1}},
    "doc without obo_id": _without("obo_id"),
    "doc without label": _without("label"),
    "obo_id a number": _with_doc(obo_id=3169),
    "label null": _with_doc(label=None),
    "label a list": _with_doc(label=["ChIP-seq"]),
    "synonym a number": _with_doc(synonym=5),
    "synonym an object": _with_doc(synonym={"0": "ChIP-exo"}),
    "synonym holding a number": _with_doc(synonym=["ChIP-exo", 5]),
    "is_defining_ontology a string": _with_doc(is_defining_ontology="true"),
    "is_defining_ontology a number": _with_doc(is_defining_ontology=1),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _BROKEN.values(), ids=_BROKEN.keys())
async def test_a_malformed_answer_is_an_outage_and_is_not_cached_as_no_match(body):
    sent: list[httpx.Request] = []
    async with _serving(sent, body, body, CHIPSEQ) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] EBI OLS \(EDAM\) returned an unparseable "
            r"200 body after 2 tries: UpstreamEnvelopeError\(.no OLS search docs in ",
        ):
            await assay.resolve_edam(c, "ChIP-seq")
        # Positive control, and the failure was not cached: the real answer is read.
        info = await assay.resolve_edam(c, "ChIP-seq")
    assert info is not None and info.edam_id == "EDAM:topic_3169"
    assert len(sent) == 3


_OPTIONAL = {
    "no synonym": (_without("synonym"), ()),
    "synonym one string": (_with_doc(synonym="ChIP-exo"), ("ChIP-exo",)),
    "synonym empty": (_with_doc(synonym=[]), ()),
    "no is_defining_ontology": (_without("is_defining_ontology"), tuple(_DOC["synonym"])),
    "not defining": (_with_doc(is_defining_ontology=False), tuple(_DOC["synonym"])),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "synonyms"), _OPTIONAL.values(), ids=_OPTIONAL.keys())
async def test_a_doc_missing_an_optional_field_is_read(body, synonyms):
    sent: list[httpx.Request] = []
    async with _serving(sent, body) as c:
        info = await assay.resolve_edam(c, "ChIP-seq")
    assert info == assay.EdamInfo("EDAM:topic_3169", "ChIP-seq", synonyms)
    assert len(sent) == 1


_JSON_VALUES = [None, True, 0, 1.5, "", "x", [], ["x"], [1], {}, {"x": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_DOC) + ["is_obsolete"])
async def test_no_wrong_typed_field_escapes_as_a_bare_error(field):
    # Every field of a real doc, with every JSON type: refused as an outage, or read.
    for value in _JSON_VALUES:
        assay._CACHE.clear()
        sent: list[httpx.Request] = []
        async with _serving(sent, _with_doc(**{field: value})) as c:
            try:
                info = await assay.resolve_edam(c, "ChIP-seq")
            except UpstreamUnavailableError:
                continue
        assert info is None or info.edam_id == "EDAM:topic_3169", (field, value)


# --- live -------------------------------------------------------------------------

# "Genes" is a synonym of the topic "Genetics", which the old 10-row relevance
# window missed (2026-10-02: 59th of 168), next to names OLS ranked first, a name
# whose data_ term comes first, and a name with spaces around it.
_LIVE_NAMES = [
    ("Genes", "EDAM:topic_3053"),
    ("ChIP-seq", "EDAM:topic_3169"),
    ("Protein structure", "EDAM:topic_2814"),
    (" RNA-seq ", "EDAM:topic_3170"),
]


@live_only
@pytest.mark.asyncio
async def test_live_a_name_ranked_low_by_relevance_is_still_found():
    async with httpx.AsyncClient() as c:
        found = [await assay.resolve_edam(c, name) for name, _ in _LIVE_NAMES]
        missing = await assay.resolve_edam(c, "zzznotanassay")
    assert [getattr(i, "edam_id", None) for i in found] == [want for _, want in _LIVE_NAMES]
    assert missing is None


@live_only
@pytest.mark.asyncio
async def test_live_edam_answers_pass_the_check():
    # The check must refuse none of what OLS really sends for EDAM: a wide
    # (non-exact) search returns hundreds of docs across EDAM's id-classes.
    from data_aggregator_mcp import _ols

    async with httpx.AsyncClient() as c:
        body = await _http.request_json(
            c,
            "GET",
            _ols.SEARCH,
            service="EBI OLS (live check)",
            params={
                "q": "sequence",
                "ontology": "edam",
                "fieldList": _ols._QUERY["fieldList"],
                "rows": "500",
            },
            expect=dict,
        )
    _ols._check_search(body)
    assert len(body["response"]["docs"]) == 500
