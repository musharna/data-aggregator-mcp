"""What the ontology lookups (``anatomy``, ``chemistry``) send to EBI OLS4 and how they
pick a term, pinned exactly for both."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _http, anatomy, chemistry
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test__ols_answers import ASPIRIN, LIVER

_UA = "data-aggregator-mcp (https://github.com/musharna/data-aggregator-mcp)"
_TAIL = (
    "&exact=true&queryFields=label%2Csynonym"
    "&fieldList=obo_id%2Clabel%2Csynonym%2Cis_defining_ontology%2Cis_obsolete&rows=10"
)


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


def _recording(sent: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_each_request_is_exactly_what_ols_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=LIVER)) as c:
        await anatomy.resolve_uberon(c, "  Liver\t")
    async with _recording(sent, httpx.Response(200, json=ASPIRIN)) as c:
        await chemistry.resolve_chebi(c, " aspirin ")
    assert [(r.method, str(r.url)) for r in sent] == [
        ("GET", "https://www.ebi.ac.uk/ols4/api/search?q=Liver&ontology=uberon" + _TAIL),
        ("GET", "https://www.ebi.ac.uk/ols4/api/search?q=aspirin&ontology=chebi" + _TAIL),
    ]
    assert [r.headers["User-Agent"] for r in sent] == [_UA, _UA]


_LOOKUPS = {
    "anatomy": (lambda c, n: anatomy.resolve_uberon(c, n), "EBI OLS \\(UBERON\\)"),
    "chemistry": (lambda c, n: chemistry.resolve_chebi(c, n), "EBI OLS \\(ChEBI\\)"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("resolve", "service"), _LOOKUPS.values(), ids=_LOOKUPS.keys())
async def test_a_failing_search_names_its_lookup_after_two_tries(resolve, service):
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=rf"^\[UpstreamUnavailableError\] {service} exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await resolve(c, "liver")
    assert len(sent) == 2


# --- picking a term ---------------------------------------------------------------

_PICKS = {
    "anatomy": (anatomy._pick_uberon, "UBERON:0002107", "liver", "uberon_id"),
    "chemistry": (chemistry._pick_chebi, "CHEBI:27732", "caffeine", "chebi_id"),
}


def _skipped(term_id: str, label: str) -> dict[str, Any]:
    return {
        "not a doc": "x",
        "foreign id": {"obo_id": "PR:000050567", "label": label},
        "obsolete": {"obo_id": term_id, "label": label, "is_obsolete": True},
        "label not a string": {"obo_id": term_id, "label": None},
        "not a match": {"obo_id": term_id, "label": f"lobe of {label}"},
    }


@pytest.mark.parametrize(("pick", "term_id", "label", "attr"), _PICKS.values(), ids=_PICKS.keys())
@pytest.mark.parametrize(
    "reason", ["not a doc", "foreign id", "obsolete", "label not a string", "not a match"]
)
def test_a_skipped_doc_does_not_end_the_search(pick, term_id, label, attr, reason):
    later = {"obo_id": term_id, "label": label}
    assert pick([later], label) is not None  # positive control: the later doc alone matches
    info = pick(
        [_skipped("X:1" if reason == "foreign id" else term_id, label)[reason], later], label
    )
    assert info is not None and getattr(info, attr) == term_id


@pytest.mark.parametrize(("pick", "term_id", "label", "attr"), _PICKS.values(), ids=_PICKS.keys())
def test_the_first_defining_match_wins_else_the_first_match(pick, term_id, label, attr):
    prefix = term_id.split(":")[0]

    def doc(n: int, defining: bool | None) -> dict[str, Any]:
        d: dict[str, Any] = {"obo_id": f"{prefix}:{n}", "label": label}
        if defining is not None:
            d["is_defining_ontology"] = defining
        return d

    def picked(*docs: dict[str, Any]) -> str:
        return getattr(pick(list(docs), label), attr)

    assert picked(doc(1, False), doc(2, True), doc(3, True)) == f"{prefix}:2"
    assert picked(doc(1, True), doc(2, True)) == f"{prefix}:1"
    assert picked(doc(1, False), doc(2, None)) == f"{prefix}:1"
    assert picked(doc(1, None), doc(2, False), doc(3, True)) == f"{prefix}:3"
