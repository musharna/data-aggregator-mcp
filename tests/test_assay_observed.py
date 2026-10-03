"""What the ``assay`` lookup sends to EBI OLS4 and how it picks an EDAM term, pinned exactly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from data_aggregator_mcp import _http, assay
from data_aggregator_mcp.errors import UpstreamUnavailableError
from tests.test__ols_observed import _TAIL, _UA, _recording
from tests.test_assay_answers import CHIPSEQ


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    async def _no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_wait)
    assay._CACHE.clear()
    yield
    assay._CACHE.clear()


@pytest.mark.asyncio
async def test_the_request_is_exactly_what_ols_is_sent():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(200, json=CHIPSEQ)) as c:
        info = await assay.resolve_edam(c, "  ChIP-seq\t")
    assert info is not None and info.edam_id == "EDAM:topic_3169"
    assert [(r.method, str(r.url)) for r in sent] == [
        ("GET", "https://www.ebi.ac.uk/ols4/api/search?q=ChIP-seq&ontology=edam" + _TAIL),
    ]
    assert [r.headers["User-Agent"] for r in sent] == [_UA]


@pytest.mark.asyncio
async def test_a_failing_search_names_the_edam_lookup_after_two_tries():
    sent: list[httpx.Request] = []
    async with _recording(sent, httpx.Response(503)) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] EBI OLS \(EDAM\) exhausted 2 retries "
            r"\(last HTTP 503\)$",
        ):
            await assay.resolve_edam(c, "ChIP-seq")
    assert len(sent) == 2


def test_only_the_ols_helper_sends_an_ols_search():
    # The OLS request was copied into anatomy, chemistry and assay, and #207's fix
    # reached two of the three. A module that builds its own OLS request is a copy
    # the next fix can miss; it must go through _ols.exact_search.
    package = Path(assay.__file__).parent
    senders = sorted(
        p.name for p in package.glob("*.py") if "ebi.ac.uk/ols4" in p.read_text(encoding="utf-8")
    )
    assert senders == ["_ols.py"]  # and the scan does see the one that does


# --- picking a term ---------------------------------------------------------------

_LATER = {"obo_id": "EDAM:topic_3169", "label": "ChIP-seq"}
_SKIPPED: dict[str, Any] = {
    "not a doc": "x",
    "foreign id": {"obo_id": "OBI:0000716", "label": "ChIP-seq"},
    "not a topic": {"obo_id": "EDAM:data_3917", "label": "ChIP-seq"},
    "obsolete": {"obo_id": "EDAM:topic_0001", "label": "ChIP-seq", "is_obsolete": True},
    "label not a string": {"obo_id": "EDAM:topic_0001", "label": None},
    "not a match": {"obo_id": "EDAM:topic_0001", "label": "ChIP-on-chip"},
}


@pytest.mark.parametrize("skipped", _SKIPPED.values(), ids=_SKIPPED.keys())
def test_a_skipped_doc_does_not_end_the_search(skipped):
    assert assay._pick_edam([skipped], "chip-seq") is None  # the doc alone is skipped
    info = assay._pick_edam([skipped, _LATER], "chip-seq")
    assert info is not None and info.edam_id == "EDAM:topic_3169"


def test_the_first_defining_match_wins_else_the_first_match():
    def doc(n: int, defining: bool | None) -> dict[str, Any]:
        d: dict[str, Any] = {"obo_id": f"EDAM:topic_{n}", "label": "ChIP-seq"}
        if defining is not None:
            d["is_defining_ontology"] = defining
        return d

    def picked(*docs: dict[str, Any]) -> str | None:
        info = assay._pick_edam(list(docs), "chip-seq")
        return info.edam_id if info else None

    assert picked(doc(1, False), doc(2, True), doc(3, True)) == "EDAM:topic_2"
    assert picked(doc(1, True), doc(2, True)) == "EDAM:topic_1"
    assert picked(doc(1, False), doc(2, None)) == "EDAM:topic_1"
    assert picked(doc(1, None), doc(2, False), doc(3, True)) == "EDAM:topic_3"
