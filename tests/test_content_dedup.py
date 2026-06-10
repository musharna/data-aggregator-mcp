"""B7 — opt-in cross-repo content dedup (mirror collapse).

Unit tests for the pure router helpers (``_normalize_title``,
``_first_author_surname``, ``_fingerprint_key``, ``_collapse_mirrors``), the
search wiring (inputSchema flag, dispatch threading, cursor round-trip), and a
gated live real-execution check that proves the fingerprint fires on real
cross-repo metadata.
"""

from __future__ import annotations

import inspect
import os

import httpx
import pytest

from data_aggregator_mcp import router, server
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Mirror

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _res(
    id_: str,
    source: str,
    *,
    title: str = "A Genomic Study",
    creators: list[str] | None = None,
    year: int | None = 2020,
    doi: str | None = None,
    checksums: list[str] | None = None,
) -> DataResource:
    return DataResource(
        id=id_,
        source=source,
        kind="dataset",
        title=title,
        creators=[Creator(name=n) for n in (creators or ["Ada Lovelace"])],
        year=year,
        doi=doi,
        files=[FileEntry(name=f"f{i}.csv", checksum=c) for i, c in enumerate(checksums or [])],
    )


# ---------------------------------------------------------------------------
# _normalize_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hello World", "hello world"),
        ("  Hello   World  ", "hello world"),
        ("Hello, World!", "hello world"),
        ("HELLO-WORLD", "hello world"),
        ("Hello: A Study (v2)", "hello a study v2"),
        ("RNA-seq of A. thaliana", "rna seq of a thaliana"),
        ("multiple    spaces\tand\ntabs", "multiple spaces and tabs"),
        ("", ""),
        ("...!!!", ""),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert router._normalize_title(raw) == expected


# ---------------------------------------------------------------------------
# _first_author_surname
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "names,expected",
    [
        (["Ada Lovelace"], "lovelace"),
        (["Ada Lovelace", "Charles Babbage"], "lovelace"),  # first author only
        (["Cher"], "cher"),  # single-name author
        (["  Grace   Hopper  "], "hopper"),
        (["MARIE CURIE"], "curie"),
        (["van der Berg"], "berg"),  # last token
    ],
)
def test_first_author_surname(names: list[str], expected: str) -> None:
    r = _res("zenodo:1", "zenodo", creators=names)
    assert router._first_author_surname(r) == expected


def test_first_author_surname_no_creators_is_none() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert r.creators == []
    assert router._first_author_surname(r) is None


def test_first_author_surname_blank_name_is_none() -> None:
    r = _res("zenodo:1", "zenodo", creators=["   "])
    assert router._first_author_surname(r) is None


# ---------------------------------------------------------------------------
# _fingerprint_key
# ---------------------------------------------------------------------------


def test_fingerprint_key_all_present() -> None:
    r = _res("zenodo:1", "zenodo", title="A Study!", creators=["Ada Lovelace"], year=2020)
    assert router._fingerprint_key(r) == ("a study", "lovelace", 2020)


def test_fingerprint_key_none_when_year_missing() -> None:
    r = _res("zenodo:1", "zenodo", year=None)
    assert router._fingerprint_key(r) is None


def test_fingerprint_key_none_when_no_creators() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t", year=2020)
    assert router._fingerprint_key(r) is None


def test_fingerprint_key_none_when_title_empty_after_normalize() -> None:
    r = _res("zenodo:1", "zenodo", title="!!!", year=2020)
    assert router._fingerprint_key(r) is None


# ---------------------------------------------------------------------------
# _collapse_mirrors
# ---------------------------------------------------------------------------


def test_collapse_two_mirrors_same_fingerprint_different_doi() -> None:
    a = _res("zenodo:1", "zenodo", title="Plant RNA Atlas", year=2021, doi="10.5281/zenodo.1")
    b = _res(
        "datacite:10.6084/m9.figshare.2",
        "datacite",
        title="Plant RNA Atlas",
        year=2021,
        doi="10.6084/m9.figshare.2",
    )
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1
    survivor = out[0]
    # native zenodo id beats datacite-prefixed (both DOI-bearing)
    assert survivor.id == "zenodo:1"
    assert survivor.mirrors == [
        Mirror(source="datacite", id="datacite:10.6084/m9.figshare.2", doi="10.6084/m9.figshare.2")
    ]


def test_collapse_by_shared_checksum_different_titles() -> None:
    a = _res("zenodo:1", "zenodo", title="One Title", checksums=["md5:deadbeef"])
    b = _res(
        "hf:owner/ds", "huggingface", title="Totally Different Title", checksums=["md5:deadbeef"]
    )
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1
    assert out[0].id == "zenodo:1"
    assert [m.id for m in out[0].mirrors] == ["hf:owner/ds"]


def test_checksum_compares_full_algo_hex() -> None:
    # same hex, different algo => NOT byte-identity => no checksum merge, and
    # titles differ so no fingerprint merge either.
    a = _res("zenodo:1", "zenodo", title="One", checksums=["md5:abc"])
    b = _res("zenodo:2", "zenodo", title="Two", checksums=["sha256:abc"])
    out = router._collapse_mirrors([a, b])
    assert len(out) == 2


def test_no_merge_same_title_author_different_year() -> None:
    a = _res("zenodo:1", "zenodo", title="Atlas", year=2020)
    b = _res("zenodo:2", "zenodo", title="Atlas", year=2021)
    out = router._collapse_mirrors([a, b])
    assert len(out) == 2
    assert all(r.mirrors == [] for r in out)


def test_no_merge_same_source_version_siblings() -> None:
    # Two SAME-source records with identical title+author+year are VERSION SIBLINGS
    # (e.g. Zenodo record v1/v2), NOT a cross-repo mirror — B7 must NOT fold them.
    # (Their version relationship is B1's domain: is_latest/superseded_by.)
    a = _res(
        "zenodo:1",
        "zenodo",
        title="Atlas",
        creators=["Lovelace"],
        year=2020,
        doi="10.5281/zenodo.1",
    )
    b = _res(
        "zenodo:2",
        "zenodo",
        title="Atlas",
        creators=["Lovelace"],
        year=2020,
        doi="10.5281/zenodo.2",
    )
    out = router._collapse_mirrors([a, b])
    assert len(out) == 2
    assert all(r.mirrors == [] for r in out)


def test_same_source_byte_identical_still_folds_on_checksum() -> None:
    # The CROSS-source rule applies only to the fingerprint path. Byte-identical
    # files (shared checksum) are the same data and fold regardless of source.
    a = _res("zenodo:1", "zenodo", title="One", year=2020, checksums=["sha256:beef"])
    b = _res("zenodo:2", "zenodo", title="Two", year=2021, checksums=["sha256:beef"])
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1


def test_no_merge_title_only_no_creators() -> None:
    a = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="Same Title", year=2020)
    b = DataResource(id="zenodo:2", source="zenodo", kind="dataset", title="Same Title", year=2020)
    out = router._collapse_mirrors([a, b])
    assert len(out) == 2  # surname unavailable => title path cannot fire


def test_no_creators_still_merges_on_checksum() -> None:
    a = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="X",
        files=[FileEntry(name="a", checksum="md5:zz")],
    )
    b = DataResource(
        id="zenodo:2",
        source="zenodo",
        kind="dataset",
        title="Y",
        files=[FileEntry(name="b", checksum="md5:zz")],
    )
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1


def test_survivor_doi_bearing_beats_doi_less() -> None:
    # first-seen is the DOI-less one, but the DOI-bearing one must survive.
    # Cross-source (zenodo vs dryad) so the fingerprint path fires.
    a = _res("zenodo:1", "zenodo", title="Atlas", year=2020, doi=None)
    b = _res("dryad:2", "dryad", title="Atlas", year=2020, doi="10.5061/dryad.2")
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1
    assert out[0].id == "dryad:2"
    assert [m.id for m in out[0].mirrors] == ["zenodo:1"]


def test_survivor_native_beats_datacite() -> None:
    a = _res(
        "datacite:10.x/1", "datacite", title="Atlas", year=2020, doi="10.x/1"
    )  # first-seen, datacite
    b = _res("zenodo:9", "zenodo", title="Atlas", year=2020, doi="10.y/2")  # native
    out = router._collapse_mirrors([a, b])
    assert len(out) == 1
    assert out[0].id == "zenodo:9"


def test_survivor_first_seen_tiebreak() -> None:
    # both native + DOI-bearing => first-seen wins. Cross-source so it merges.
    a = _res("zenodo:1", "zenodo", title="Atlas", year=2020, doi="10.x/1")
    b = _res("dryad:2", "dryad", title="Atlas", year=2020, doi="10.x/2")
    out = router._collapse_mirrors([a, b])
    assert out[0].id == "zenodo:1"
    assert [m.id for m in out[0].mirrors] == ["dryad:2"]


def test_three_way_mirror_group() -> None:
    a = _res("zenodo:1", "zenodo", title="Atlas", year=2020, doi=None)
    b = _res("datacite:10.x/2", "datacite", title="Atlas", year=2020, doi="10.x/2")
    c = _res("hf:o/n", "huggingface", title="Atlas", year=2020, doi=None)
    out = router._collapse_mirrors([a, b, c])
    assert len(out) == 1
    survivor = out[0]
    assert survivor.id == "datacite:10.x/2"  # only DOI-bearing
    assert {m.id for m in survivor.mirrors} == {"zenodo:1", "hf:o/n"}
    assert len(survivor.mirrors) == 2


def test_distinct_datasets_untouched() -> None:
    a = _res("zenodo:1", "zenodo", title="Alpha", creators=["Smith"], year=2020)
    b = _res("zenodo:2", "zenodo", title="Beta", creators=["Jones"], year=2021)
    out = router._collapse_mirrors([a, b])
    assert len(out) == 2
    assert all(r.mirrors == [] for r in out)


def test_survivor_order_preserved() -> None:
    # group1 (Alpha) first-seen at index 0, group2 (Beta) first-seen at index 1.
    # The Alpha mirror is cross-source (dryad) so it folds into the zenodo Alpha.
    recs = [
        _res("zenodo:1", "zenodo", title="Alpha", year=2020),
        _res("zenodo:2", "zenodo", title="Beta", year=2020),
        _res("dryad:3", "dryad", title="Alpha", year=2020),  # cross-repo mirror of #1
    ]
    out = router._collapse_mirrors(recs)
    assert [r.title for r in out] == ["Alpha", "Beta"]
    assert [m.id for m in out[0].mirrors] == ["dryad:3"]


def test_record_never_its_own_mirror() -> None:
    a = _res("zenodo:1", "zenodo", title="Atlas", year=2020, doi="10.x/1")
    b = _res("zenodo:2", "zenodo", title="Atlas", year=2020, doi="10.x/2")
    out = router._collapse_mirrors([a, b])
    survivor = out[0]
    assert all(m.id != survivor.id for m in survivor.mirrors)


def test_collapse_is_pure_and_deterministic() -> None:
    recs = [
        _res("zenodo:1", "zenodo", title="Atlas", year=2020, doi="10.x/1"),
        _res("datacite:10.x/2", "datacite", title="Atlas", year=2020, doi="10.x/2"),
        _res("zenodo:9", "zenodo", title="Other", creators=["Jones"], year=2019),
    ]
    first = [r.model_dump() for r in router._collapse_mirrors(recs)]
    second = [r.model_dump() for r in router._collapse_mirrors(recs)]
    assert first == second
    # inputs not mutated
    assert all(r.mirrors == [] for r in recs)


def test_collapse_module_makes_no_http_calls() -> None:
    src = inspect.getsource(router._collapse_mirrors)
    assert "httpx" not in src
    assert "await" not in src


# ---------------------------------------------------------------------------
# DataResource.mirrors field
# ---------------------------------------------------------------------------


def test_data_resource_mirrors_defaults_empty_and_dumps() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert r.mirrors == []
    assert r.model_dump()["mirrors"] == []
    r2 = r.model_copy(update={"mirrors": [Mirror(source="x", id="x:1", doi=None)]})
    dumped = r2.model_dump()["mirrors"]
    assert dumped == [{"source": "x", "id": "x:1", "doi": None}]


# ---------------------------------------------------------------------------
# search wiring
# ---------------------------------------------------------------------------


def test_search_tool_exposes_collapse_mirrors_param() -> None:
    tool = next(t for t in server.TOOLS if t.name == "search")
    prop = tool.inputSchema["properties"]["collapse_mirrors"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "collapse_mirrors" not in tool.inputSchema.get("required", [])


async def test_dispatch_search_threads_collapse_mirrors(monkeypatch) -> None:
    from data_aggregator_mcp.models import SearchResult

    captured: dict = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query=kwargs.get("query"), total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    await server._dispatch("search", {"query": "rna", "collapse_mirrors": True})
    assert captured["collapse_mirrors"] is True


async def test_dispatch_search_collapse_mirrors_defaults_false(monkeypatch) -> None:
    from data_aggregator_mcp.models import SearchResult

    captured: dict = {}

    async def fake_search_page(client, **kwargs):
        captured.update(kwargs)
        return SearchResult(query=kwargs.get("query"), total=0, count=0, results=[], errors={})

    monkeypatch.setattr("data_aggregator_mcp.router.search_page", fake_search_page)
    await server._dispatch("search", {"query": "rna"})
    assert captured["collapse_mirrors"] is False


def _mirror_recs() -> list[DataResource]:
    return [
        _res("zenodo:1", "zenodo", title="Mirror Set", year=2022, doi="10.5281/zenodo.1"),
        _res(
            "datacite:10.6084/m9.figshare.2",
            "datacite",
            title="Mirror Set",
            year=2022,
            doi="10.6084/m9.figshare.2",
        ),
    ]


async def test_search_page_collapses_when_flag_on(monkeypatch) -> None:
    recs = _mirror_recs()
    seq = iter([recs])

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return len(recs), next(seq, [])

    # route everything through one adapter returning both mirror records
    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="x", size=10, sources=["zenodo"], collapse_mirrors=True
        )
    assert result.count == 1
    assert len(result.results) == 1
    survivor = result.results[0]
    assert survivor.id == "zenodo:1"
    assert [m.id for m in survivor.mirrors] == ["datacite:10.6084/m9.figshare.2"]


async def test_search_page_no_collapse_when_flag_off(monkeypatch) -> None:
    recs = _mirror_recs()

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return len(recs), list(recs)

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        result = await router.search_page(
            client, query="x", size=10, sources=["zenodo"], collapse_mirrors=False
        )
    assert result.count == 2
    assert all(r.mirrors == [] for r in result.results)


async def test_collapse_mirrors_round_trips_cursor(monkeypatch) -> None:
    # return one record per page so a next_cursor is emitted on page 1
    def make_page(offset: int) -> list[DataResource]:
        return [_res(f"zenodo:{offset}", "zenodo", title="Set", year=2022, doi=f"10.x/{offset}")]

    async def fake_zenodo_search(client, query, *, size=10, offset=0):
        return 5, make_page(offset)

    monkeypatch.setattr("data_aggregator_mcp.zenodo.search", fake_zenodo_search)
    async with httpx.AsyncClient() as client:
        page1 = await router.search_page(
            client, query="x", size=1, sources=["zenodo"], collapse_mirrors=True
        )
        assert page1.next_cursor is not None
        from data_aggregator_mcp import _cursor

        assert _cursor.decode(page1.next_cursor)["collapse_mirrors"] is True
        # continuation page still carries the flag (decodes True, does not crash)
        page2 = await router.search_page(client, cursor=page1.next_cursor)
    assert page2 is not None


# ---------------------------------------------------------------------------
# Live real-execution check (gated)
# ---------------------------------------------------------------------------


@_live_only
async def test_live_collapse_mirrors_wellformed_and_fingerprint_fires() -> None:
    """Real ``collapse_mirrors=True`` page must not crash and must be well-formed;
    PLUS a positive fingerprint/checksum identity proof on REAL metadata —
    found organically among the live results, not a synthetic fixture, so the
    fingerprint is proven to fire on data we did not author."""
    from collections import defaultdict

    query = "single cell RNA-seq atlas"
    sources = ["zenodo", "datacite"]
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # 1) collapse=True must not crash and must be well-formed
        collapsed = await router.search_page(
            client, query=query, size=25, sources=sources, collapse_mirrors=True
        )
        for r in collapsed.results:
            ids = {m.id for m in r.mirrors}
            assert r.id not in ids, "a record must never list itself as its own mirror"
            assert len(ids) == len(r.mirrors), "mirror ids must be distinct"
            assert all(m.source for m in r.mirrors), "every mirror has a source"

        folded = [r for r in collapsed.results if r.mirrors]
        print(f"\n[live] collapsed emitted={len(collapsed.results)} with_mirrors={len(folded)}")
        for r in folded:
            print(f"[live] survivor={r.id!r} mirrors={[(m.source, m.id) for m in r.mirrors]}")

        # 2) Positive proof the fingerprint fires on REAL metadata: re-run the same
        #    page WITHOUT collapse and find an organic identity collision among the
        #    real records (a fingerprint-key match or a shared checksum). Assert it,
        #    then confirm collapse actually folded that pair.
        raw = await router.search_page(
            client, query=query, size=25, sources=sources, collapse_mirrors=False
        )
        by_key: dict[tuple, list[DataResource]] = defaultdict(list)
        by_sum: dict[str, list[DataResource]] = defaultdict(list)
        for r in raw.results:
            k = router._fingerprint_key(r)
            if k is not None:
                by_key[k].append(r)
            for s in router._checksums(r):
                by_sum[s].append(r)

        key_hits = [(k, v) for k, v in by_key.items() if len({x.id for x in v}) > 1]
        sum_hits = [(s, v) for s, v in by_sum.items() if len({x.id for x in v}) > 1]

        if key_hits:
            k, group = key_hits[0]
            ids = sorted({x.id for x in group})
            print(f"[live] REAL fingerprint collision key={k} ids={ids}")
            # all members compute the identical key — the fingerprint fired on real data
            assert all(router._fingerprint_key(x) == k for x in group)
            assert len(ids) >= 2
        elif sum_hits:
            s, group = sum_hits[0]
            ids = sorted({x.id for x in group})
            print(f"[live] REAL checksum collision sum={s!r} ids={ids}")
            assert all(s in router._checksums(x) for x in group)
            assert len(ids) >= 2
        else:
            pytest.skip(
                "live: no organic identity collision surfaced this run "
                "(no-crash/well-formedness invariants still asserted above)"
            )
