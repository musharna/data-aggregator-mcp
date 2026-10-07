"""Sources that match words exactly are sent each word with its plural.

Head-to-head round 2 (2026-10-05), snow leopard task: DataCite found "Data for 'Spatial
ecology of snow leopards ... Altai'" for "snow leopards" but never for "snow leopard",
while the match tiers already counted "leopards" as naming "leopard".
"""

from __future__ import annotations

import os
import types

import httpx
import pytest

from data_aggregator_mcp import _cursor, _http, datacite, omics, router
from data_aggregator_mcp._relevance import with_plurals
from data_aggregator_mcp.models import DataResource

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_S29 = "10.6084/m9.figshare.19184897"  # the Altai study, titled with "snow leopards"
# R3's Ramazzottius varieornatus study: its records name the species, never "tardigrade"
_S1 = "PRJDB2359"


def test_each_plain_word_is_sent_with_its_plural() -> None:
    assert with_plurals("snow leopard") == "(snow OR snows) (leopard OR leopards)"
    assert with_plurals("Leopard  Altai") == "(Leopard OR Leopards)  (Altai OR Altais)"
    # inside a group too, so an agent's boolean query keeps its structure
    assert (
        with_plurals("leopard AND (cat OR panther)")
        == "(leopard OR leopards) AND ((cat OR cats) OR (panther OR panthers))"
    )
    assert with_plurals("") == ""


def test_syntax_and_words_that_need_no_plural_are_sent_as_written() -> None:
    kept = '"snow leopard" NOT title:wolf cat* ox of the cells CELLS 16S rRNA-seq +gene'
    assert with_plurals(kept) == kept
    # the organism expansion's quoted names survive beside a changed word
    assert (
        with_plurals('transcriptome AND ("Phelipanche aegyptiaca" OR "Orobanche aegyptiaca")')
        == '(transcriptome OR transcriptomes) AND ("Phelipanche aegyptiaca" OR "Orobanche aegyptiaca")'
    )


def _recording(sent: list[str], *, plurals: bool):
    """A source that, like the real ones, applies ``with_plurals`` unless told not to."""

    async def search(client, q, *, size=10, offset=0, **kw):
        assert kw.get("plurals", False) is False  # the router only ever turns it off
        sent.append(with_plurals(q) if kw.get("plurals", plurals) else q)
        hits = [
            DataResource(id=f"x:{i}", source="x", kind="dataset", title="snow leopard")
            for i in range(3)
        ]
        return 3, hits[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset(), QUERY_PLURALS=plurals)


async def test_a_cursor_minted_before_plurals_keeps_the_query_as_written(monkeypatch) -> None:
    exact: list[str] = []
    stems: list[str] = []
    monkeypatch.setattr(
        router,
        "_ADAPTERS",
        {"exact": _recording(exact, plurals=True), "stems": _recording(stems, plurals=False)},
    )
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="snow leopard", size=1)
        await router.search_page(c, cursor=page.next_cursor)
        # A cursor minted before plurals were sent has no "pl": its offsets index the
        # query as written, so it keeps being sent as written.
        old = _cursor.decode(page.next_cursor)
        old.pop("pl", None)
        await router.search_page(c, cursor=_cursor.encode(old))
    plural = "(snow OR snows) (leopard OR leopards)"
    assert exact == [plural, plural, "snow leopard"]
    assert stems == ["snow leopard"] * 3  # positive control: the other source is untouched


def _composite(sent: list[str]):
    """A source paged per sub-source, like NCBI's, that applies ``with_plurals`` unless
    told not to."""

    async def search_subsource(client, sub, q, *, size=10, offset=0, plurals=True):
        sent.append(with_plurals(q) if plurals else q)
        hits = [
            DataResource(id=f"{sub}:{i}", source=sub, kind="dataset", title="snow leopard")
            for i in range(3)
        ]
        return 3, hits[offset : offset + size]

    return types.SimpleNamespace(
        search_subsource=search_subsource,
        SUBSOURCES=("a",),
        PREFIXES=frozenset(),
        QUERY_PLURALS=True,
    )


async def test_a_cursor_minted_before_ncbi_got_plurals_keeps_its_query_there(
    monkeypatch,
) -> None:
    """NCBI's sub-sources were sent plurals after the other sources, so a cursor holding
    "pl" but no "sp" indexes the query as written there and plurals everywhere else."""
    ncbi: list[str] = []
    exact: list[str] = []
    monkeypatch.setattr(
        router, "_ADAPTERS", {"ncbi": _composite(ncbi), "exact": _recording(exact, plurals=True)}
    )
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="snow leopard", size=1)
        await router.search_page(c, cursor=page.next_cursor)
        old = _cursor.decode(page.next_cursor)
        old.pop("sp", None)
        await router.search_page(c, cursor=_cursor.encode(old))
        old.pop("pl", None)
        await router.search_page(c, cursor=_cursor.encode(old))
    plural = "(snow OR snows) (leopard OR leopards)"
    assert ncbi == [plural, plural, "snow leopard", "snow leopard"]
    # positive control: the source sent plurals first keeps them until "pl" goes too
    assert exact == [plural, plural, plural, "snow leopard"]


class _Sent(Exception):
    """Stops a search at its request, carrying the params it was about to send."""


@pytest.mark.parametrize("name", ["biostudies", "datacite", "omicsdi", "uniprot", "zenodo"])
async def test_each_declaring_source_sends_the_plurals_upstream(monkeypatch, name) -> None:
    def stop(*args, params=None, **kw):
        raise _Sent(params)

    monkeypatch.setattr(_http, "request_json", stop)
    monkeypatch.setattr(_http, "request_json_with_headers", stop)
    sent = []
    async with httpx.AsyncClient() as c:
        for plurals in (True, False):
            with pytest.raises(_Sent) as stopped:
                await router._ADAPTERS[name].search(c, "snow leopard", plurals=plurals)
            sent.append(" ".join(str(v) for v in stopped.value.args[0].values()))
    assert "(snow OR snows) (leopard OR leopards)" in sent[0]
    assert "snow leopard" in sent[1] and "leopards" not in sent[1]  # positive control


def test_the_sources_that_match_words_exactly() -> None:
    """Each probed live 2026-10-05: the OR form counted between the larger of the two
    words' counts and their sum. DataONE stems already; GBIF and NASA CMR read the OR form
    some other way. NGDC (2026-10-06): "leopard" 13, "leopards" 1, OR form 13. NCBI
    (2026-10-07) counted the OR form above the sum when it maps the plural to a taxon
    ("tardigrades" to Tardigrada) and at the singular's count when the plural is no word
    ("mouses"), never below the singular."""
    declared = {n for n, a in router._ADAPTERS.items() if getattr(a, "QUERY_PLURALS", False)}
    assert declared == {"biostudies", "datacite", "ngdc", "omics", "omicsdi", "uniprot", "zenodo"}


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_datacite_finds_a_study_titled_with_the_plural() -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        _, singular = await datacite.search(c, "snow leopard Altai", size=50, plurals=False)
        _, plural = await datacite.search(c, "snow leopards Altai", size=50, plurals=False)
        page = await router.search_page(
            c, query="snow leopard Altai", sources=["datacite"], size=50
        )
    dois = lambda recs: {(r.doi or "").lower() for r in recs}  # noqa: E731
    assert _S29 in dois(plural)  # positive control: the record is there to find
    assert _S29 not in dois(singular)  # the query as written misses it upstream
    assert _S29 in dois(page.results)


@pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")
async def test_live_ncbi_finds_a_tardigrade_study_that_never_names_the_word() -> None:
    """NCBI maps "tardigrades", not "tardigrade", to the taxon Tardigrada, and R3's
    S1 records name only the species Ramazzottius varieornatus."""
    q = "tardigrade anhydrobiosis"
    studies = lambda recs: {a for r in recs for a in r.accessions}  # noqa: E731
    async with httpx.AsyncClient(timeout=90) as c:
        _, singular = await omics.search_subsource(c, "sra", q, size=50, plurals=False)
        _, plural = await omics.search_subsource(c, "sra", q, size=50)
        page = await router.search_page(c, query=q, sources=["omics"], size=50)
    assert singular  # positive control: the query as written finds tardigrade runs
    assert _S1 not in studies(singular)  # but none of S1's
    assert _S1 in studies(plural)
    assert _S1 in studies(page.results)
