"""Cursor walks: every upstream record comes back exactly once.

Audit 2026-09-22 (H4, M5, M6, M8, M9, L19 + a continuation bug). Each walk below
pages ``search_page`` through its cursors to the end and compares the union of
pages against what the fake upstreams hold — the property a caller relies on and
the one the old per-page assertions could not see.
"""

from __future__ import annotations

import types
from collections import Counter

import httpx
import pytest

from data_aggregator_mcp import _cursor, embeddings, literature, omics, router
from data_aggregator_mcp.errors import UpstreamUnavailableError, ValidationError
from data_aggregator_mcp.models import DataResource


def _rec(src: str, i: int, doi: str | None = None) -> DataResource:
    return DataResource(id=f"{src}:{i}", source=src, kind="dataset", title=f"{src} {i}", doi=doi)


def _adapter(recs: list[DataResource], seen: list[str] | None = None):
    async def search(client, q, *, size=10, offset=0):
        if seen is not None:
            seen.append(q)
        return len(recs), recs[offset : offset + size]

    return types.SimpleNamespace(search=search, PREFIXES=frozenset())


async def _walk(first: dict, max_pages: int = 50) -> tuple[list[str], list]:
    ids: list[str] = []
    pages = []
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, **first)
        pages.append(page)
        ids += [r.id for r in page.results]
        while page.next_cursor and len(pages) < max_pages:
            page = await router.search_page(client, cursor=page.next_cursor)
            pages.append(page)
            ids += [r.id for r in page.results]
    assert len(pages) < max_pages, "cursor never terminated"
    return ids, pages


def _dupes(ids: list[str]) -> dict[str, int]:
    return {k: v for k, v in Counter(ids).items() if v > 1}


# --- H4: a composite source pages each sub-database from its own offset -------------


async def test_omics_walk_returns_every_record_of_every_subdb_once(monkeypatch) -> None:
    """omics fans out to gds/sra/bioproject but took ONE offset and applied it to each
    db, then truncated the interleave to `size`: 60 of 90 records were never returned."""
    held = {db: [_rec(db, i) for i in range(30)] for db in omics._DB.values()}

    async def fake_db(client, db, q, size, offset=0):
        return len(held[db]), held[db][offset : offset + size]

    monkeypatch.setattr(omics, "_search_db", fake_db)
    ids, _ = await _walk({"query": "x", "sources": ["omics"], "size": 10})
    everything = {r.id for recs in held.values() for r in recs}
    assert _dupes(ids) == {}
    assert set(ids) == everything, f"never returned: {len(everything - set(ids))}"


async def test_literature_walk_returns_every_record_of_both_backends_once(monkeypatch) -> None:
    held = {n: [_rec(n, i) for i in range(30)] for n in ("pubmed", "openaire")}
    monkeypatch.setattr(literature, "_BACKENDS", {n: _adapter(v) for n, v in held.items()})
    ids, _ = await _walk({"query": "x", "sources": ["literature"], "size": 10})
    assert _dupes(ids) == {}
    assert set(ids) == {r.id for recs in held.values() for r in recs}


# --- H3 (composite part): a sub-database outage is reported, not "0 results" --------


async def test_composite_subsource_failures_reach_errors(monkeypatch) -> None:
    """Every NCBI db down used to yield total=0, errors={} — "no data exists"."""

    async def down(client, db, q, size, offset=0):
        raise UpstreamUnavailableError(f"NCBI {db} HTTP 503")

    monkeypatch.setattr(omics, "_search_db", down)
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="x", sources=["omics"])
    assert page.results == []
    assert page.errors and all(k.startswith("omics") for k in page.errors), page.errors
    assert len(page.errors) == 3 and all("503" in v for v in page.errors.values())

    # Positive control + partial outage: one db answering still returns its records, and
    # the two failing dbs are named (they used to be swallowed into a log line).
    async def one_up(client, db, q, size, offset=0):
        if db == "sra":
            return 1, [_rec("sra", 0)]
        raise UpstreamUnavailableError(f"NCBI {db} HTTP 503")

    monkeypatch.setattr(omics, "_search_db", one_up)
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="x", sources=["omics"])
    assert [r.id for r in page.results] == ["sra:0"]
    assert len(page.errors) == 2 and not any("sra" in k for k in page.errors)


async def test_composite_adapter_search_raises_when_every_backend_fails(monkeypatch) -> None:
    """The adapters' own ``search`` (the SourceAdapter contract) must not return
    (0, []) for a total outage either."""

    async def lit_down(client, q, *, size=10, offset=0):
        raise UpstreamUnavailableError("503")

    down = types.SimpleNamespace(search=lit_down)
    monkeypatch.setattr(literature, "_BACKENDS", {"pubmed": down, "openaire": down})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="every backend failed"):
            await literature.search(client, "x")
        monkeypatch.setattr(
            literature, "_BACKENDS", {"pubmed": down, "openaire": _adapter([_rec("oa", 0)])}
        )
        total, recs = await literature.search(client, "x")  # positive control: partial
    assert total == 1 and [r.id for r in recs] == ["oa:0"]


# --- M5: window re-ranking must not drop the rest of the window ---------------------


@pytest.mark.parametrize("rerank", ["unavailable", "reverse"])
async def test_semantic_walk_returns_every_record_once(monkeypatch, rerank: str) -> None:
    """rank=semantic consumed the whole fetched window but emitted only `size`: with 2
    sources x 6 records and size=3, half the records were never returned."""

    async def fake_rerank(client, q, resources):
        if rerank == "reverse":
            return list(reversed(resources)), None
        return resources, "no embedding endpoint"

    monkeypatch.setattr(embeddings, "rerank", fake_rerank)
    held = {"zenodo": [_rec("zenodo", i) for i in range(6)]}
    held["dataone"] = [_rec("dataone", i) for i in range(6)]
    monkeypatch.setattr(router, "_ADAPTERS", {n: _adapter(v) for n, v in held.items()})
    ids, pages = await _walk({"query": "q", "size": 3, "rank": "semantic"})
    assert _dupes(ids) == {}
    assert set(ids) == {r.id for v in held.values() for r in v}
    if rerank == "reverse":
        # Positive control: re-ranking still reorders WITHIN a source on page 1.
        first = [r.id for r in pages[0].results]
        assert first[0] in {"zenodo:2", "dataone:2"}, first


# --- M6: a DOI-deduped record still occupies its source's offset -----------------------


async def test_doi_dedup_does_not_shift_the_losers_offset(monkeypatch) -> None:
    """zenodo:0 and datacite:0 share a DOI; the loser was dropped from the window but
    not counted as consumed, so datacite's offset lagged by one and every later page
    repeated a datacite record."""
    a = [_rec("zenodo", i, doi="10.1/x" if i == 0 else None) for i in range(4)]
    b = [_rec("datacite", i, doi="10.1/x" if i == 0 else None) for i in range(4)]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(a), "datacite": _adapter(b)})
    ids, _ = await _walk({"query": "q", "size": 3})
    assert _dupes(ids) == {}
    # Every record except the deduped mirror, exactly once (positive control: the DOI
    # winner IS returned).
    assert set(ids) == {r.id for r in a + b[1:]}


async def test_records_without_doi_keep_their_place_in_the_walk(monkeypatch) -> None:
    """dedup_by_doi moves no-DOI records to the END of the window, so a cut before them
    counted DOI records from deeper in the stream and skipped / repeated records."""
    a = [_rec("zenodo", i, doi=f"10.z/{i}" if i % 2 else None) for i in range(9)]
    b = [_rec("dataone", i, doi=f"10.d/{i}") for i in range(9)]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(a), "dataone": _adapter(b)})
    ids, _ = await _walk({"query": "q", "size": 4})
    assert _dupes(ids) == {}
    assert set(ids) == {r.id for r in a + b}


# --- multi-query: the same window-consumption loss as M5 ---------------------------------


async def test_multi_query_walk_returns_every_record_once(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setattr(router.query_understanding_mod, "expand", AsyncMock(return_value=["alt"]))
    held = {q: [_rec(f"zenodo-{q}", i) for i in range(5)] for q in ("orig", "alt")}

    async def search(client, q, *, size=10, offset=0):
        return len(held[q]), held[q][offset : offset + size]

    monkeypatch.setattr(
        router, "_ADAPTERS", {"zenodo": types.SimpleNamespace(search=search, PREFIXES=())}
    )
    ids, _ = await _walk({"query": "orig", "size": 3, "multi_query": True})
    assert _dupes(ids) == {}
    assert set(ids) == {r.id for v in held.values() for r in v}


# --- continuation must search the same (expanded) query as page 1 ----------------------


async def test_continuation_searches_the_expanded_query_not_the_raw_one(monkeypatch) -> None:
    """The cursor stored the raw query and page 2+ searched it UNEXPANDED: an
    organism-restricted walk silently widened to every organism after page 1, so its
    offsets indexed a different result set."""
    from data_aggregator_mcp import taxonomy

    info = taxonomy.TaxonInfo(
        taxid=4577, canonical_name="Zea mays", synonyms=("maize",), is_plant=True
    )

    async def fake_resolve_taxon(client, name):
        return info

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve_taxon)
    seen: list[str] = []
    recs = [_rec("zenodo", i) for i in range(4)]
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter(recs, seen)})
    ids, pages = await _walk({"query": "rna", "organism": "maize", "size": 2})
    assert len(pages) == 2 and len(seen) == 2
    assert seen[0] == seen[1], seen
    assert "Zea mays" in seen[0]  # positive control: page 1 was expanded at all
    assert sorted(ids) == sorted(r.id for r in recs)


# --- M9: keyword-only sources get the plain query ------------------------------------


async def test_keyword_only_sources_receive_the_unexpanded_query(monkeypatch) -> None:
    """The boolean-expanded query ('(q) AND ("Zea mays" OR "maize")') returned 0 from
    cellxgene / HuggingFace / OpenML and HTTP 400 from NASA CMR, none of which parse
    boolean syntax. They now get the plain query, and the page says so."""
    from data_aggregator_mcp import taxonomy

    info = taxonomy.TaxonInfo(
        taxid=4577, canonical_name="Zea mays", synonyms=("maize",), is_plant=True
    )

    async def fake_resolve_taxon(client, name):
        return info

    monkeypatch.setattr(taxonomy, "resolve_taxon", fake_resolve_taxon)
    seen: dict[str, list[str]] = {"zenodo": [], "huggingface": [], "nasacmr": []}
    monkeypatch.setattr(
        router,
        "_ADAPTERS",
        {n: _adapter([_rec(n, 0)], seen[n]) for n in seen},
    )
    async with httpx.AsyncClient() as client:
        page = await router.search_page(client, query="genome", organism="maize")
    assert seen["huggingface"] == ["genome"] and seen["nasacmr"] == ["genome"]
    # Positive control: a boolean-capable source still gets the expansion.
    assert seen["zenodo"][0].startswith("(genome) AND (") and "Zea mays" in seen["zenodo"][0]
    note = page.errors.get("query_syntax", "")
    assert "huggingface" in note and "nasacmr" in note and "zenodo" not in note

    # And without an expansion there is nothing to report.
    async with httpx.AsyncClient() as client:
        plain = await router.search_page(client, query="genome")
    assert "query_syntax" not in plain.errors


# --- M8: DOI spellings route like the bare DOI -----------------------------------------


@pytest.mark.parametrize(
    "given",
    ["doi:10.1234/ABC", "https://doi.org/10.1234/ABC", "http://dx.doi.org/10.1234/ABC"],
)
async def test_resolve_accepts_common_doi_spellings(monkeypatch, given: str) -> None:
    """`doi:` / `https://doi.org/` reached datacite.resolve verbatim → false NotFound."""
    router._RESOLVE_CACHE.clear()
    got: list[str] = []

    async def fake_datacite_resolve(client, rid):
        got.append(rid)
        return DataResource(id=f"datacite:{rid}", source="datacite", kind="dataset", title="t")

    monkeypatch.setattr(router.datacite, "resolve", fake_datacite_resolve)
    async with httpx.AsyncClient() as client:
        await router.resolve(client, given)
        router._RESOLVE_CACHE.clear()
        await router.resolve(client, "10.1234/ABC")  # positive control: bare DOI
    assert got == ["10.1234/ABC", "10.1234/ABC"], got
    router._RESOLVE_CACHE.clear()


# --- L19: sources=[] is a caller error, not a successful empty search -----------------


async def test_empty_sources_list_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(router, "_ADAPTERS", {"zenodo": _adapter([_rec("zenodo", 0)])})
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValidationError, match="at least one source"):
            await router.search_page(client, query="q", sources=[])
        ok = await router.search_page(client, query="q", sources=["zenodo"])
    assert [r.id for r in ok.results] == ["zenodo:0"]


# --- cursor hardening for the new state --------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"offsets": {"zenodo": "x"}},
        {"offsets": {"zenodo": -1}},
        {"ahead": {"zenodo": [-2]}},
        {"ahead": {"zenodo": "0"}},
        {"eq": 5},
    ],
)
def test_cursor_rejects_malformed_paging_state(bad: dict) -> None:
    base = {"q": "q", "size": 3, "offsets": {"zenodo": 3}}
    assert _cursor.decode(_cursor.encode(base))["offsets"] == {"zenodo": 3}  # control
    with pytest.raises(ValidationError):
        _cursor.decode(_cursor.encode({**base, **bad}))
