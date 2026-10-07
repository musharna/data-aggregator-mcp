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
_S7 = "PRJNA79663"  # Milnesium tardigradum; its runs name "tardigrade anhydrobiosis"


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


_WORDS = [
    DataResource(id=f"w:{i}", source="w", kind="dataset", title="snow leopard") for i in range(4)
]
_TAXON = [
    DataResource(id=f"t:{i}", source="t", kind="dataset", title="Panthera uncia")
    for i in range(100)
]


def _ncbi(sent: list[str]):
    """Answers like an NCBI db: the query as written finds the records naming its words,
    the plural form also the records NCBI maps to a taxon, and those come first, as NCBI
    orders newest first whatever the query."""

    async def search_subsource(client, sub, q, *, size=10, offset=0):
        sent.append(q)
        recs = _TAXON if " NOT " in q else _TAXON + _WORDS if " OR " in q else _WORDS
        return len(recs), recs[offset : offset + size]

    return types.SimpleNamespace(
        search_subsource=search_subsource,
        SUBSOURCES=("sra",),
        PREFIXES=frozenset(),
        PLURAL_STREAM=True,
    )


async def test_ncbi_records_matched_through_a_plural_join_those_naming_every_word(
    monkeypatch,
) -> None:
    """Sent as one query, the plural form's taxon matches fill the window fetched and the
    records naming every word never reach the page; sent as written, the taxon matches
    are never found (R3: S7 fell from 1st to 34th in SRA; S1 was out of reach)."""
    sent: list[str] = []
    monkeypatch.setattr(router, "_ADAPTERS", {"ncbi": _ncbi(sent)})
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="snow leopard", size=10)
    ids = [r.id for r in page.results]
    assert ids[:4] == [r.id for r in _WORDS]
    assert ids[4:] and all(i.startswith("t:") for i in ids[4:])
    assert sorted(set(sent)) == [
        "((snow OR snows) (leopard OR leopards)) NOT (snow leopard)",
        "snow leopard",
    ]


async def test_a_cursor_minted_before_the_plural_streams_has_none(monkeypatch) -> None:
    """A cursor holding "pl" but no "ps" was minted when NCBI got the query as written
    only, so it holds no offsets for a plural stream and is continued without one."""
    ncbi: list[str] = []
    exact: list[str] = []
    monkeypatch.setattr(
        router, "_ADAPTERS", {"ncbi": _ncbi(ncbi), "exact": _recording(exact, plurals=True)}
    )
    extra = "((snow OR snows) (leopard OR leopards)) NOT (snow leopard)"
    plural = "(snow OR snows) (leopard OR leopards)"
    async with httpx.AsyncClient() as c:
        page = await router.search_page(c, query="snow leopard", size=1)
        assert sorted(ncbi) == [extra, "snow leopard"]
        ncbi.clear()
        await router.search_page(c, cursor=page.next_cursor)
        assert sorted(ncbi) == [extra, "snow leopard"]
        ncbi.clear()
        old = _cursor.decode(page.next_cursor)
        old.pop("ps", None)
        await router.search_page(c, cursor=_cursor.encode(old))
        assert ncbi == ["snow leopard"]
        ncbi.clear()
        old.pop("pl", None)
        await router.search_page(c, cursor=_cursor.encode(old))
        assert ncbi == ["snow leopard"]
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
    ("tardigrades" to Tardigrada), never below the singular, but orders its records
    newest first, so it is sent what the plurals add as a stream of its own."""
    declared = {n for n, a in router._ADAPTERS.items() if getattr(a, "QUERY_PLURALS", False)}
    assert declared == {"biostudies", "datacite", "ngdc", "omicsdi", "uniprot", "zenodo"}
    streamed = {n for n, a in router._ADAPTERS.items() if getattr(a, "PLURAL_STREAM", False)}
    assert streamed == {"omics"}


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
    """NCBI maps "tardigrades", not "tardigrade", to the taxon Tardigrada, and R3's S1
    records name only the species Ramazzottius varieornatus. Sent in one query the 33 runs
    that adds put S7's runs, which name both words, 34th to 37th in SRA."""
    q = "tardigrade anhydrobiosis"
    studies = lambda recs: {a for r in recs for a in r.accessions}  # noqa: E731
    async with httpx.AsyncClient(timeout=90) as c:
        _, singular = await omics.search_subsource(c, "sra", q, size=50)
        _, extra = await omics.search_subsource(c, "sra", f"({with_plurals(q)}) NOT ({q})", size=50)
        page = await router.search_page(c, query=q, sources=["omics"], size=30)
    assert _S7 in studies(singular)  # positive control: the query as written finds S7
    assert _S1 not in studies(singular)  # but not S1
    assert _S1 in studies(extra) and _S7 not in studies(extra)
    assert {_S1, _S7} <= studies(page.results)
