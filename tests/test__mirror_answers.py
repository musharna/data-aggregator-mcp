"""Mirror collapse on the record shapes the upstreams actually serve.

The pairs below are live DataCite/Zenodo records (probed 2026-10-02): two copies of one
deposit whose first author is written in the two orders repositories use, and distinct
datasets that share a generic title, a year and one word of the first author's name.
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_aggregator_mcp import _mirror, router
from data_aggregator_mcp.models import Creator, DataResource, Mirror

_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


def _rec(id_: str, source: str, title: str, author: str, year: int) -> DataResource:
    """A record as the adapter returns it; ``id_`` is ``<prefix>:<DOI>``."""
    return DataResource(
        id=id_,
        source=source,
        kind="dataset",
        title=title,
        creators=[Creator(name=author)],
        year=year,
        doi=id_.split(":", 1)[1]
        if id_.startswith("datacite:")
        else "10.5281/" + id_.replace(":", "."),
    )


SYNAPSE = "Synapse Open Dataset: A dataset for warehouse robots"
RG = "datacite:10.13140/rg.2.2.12804.54408"
# The same deposit on Zenodo ("Family, Given") and ResearchGate ("Given Family").
SAME_DATASET = [
    (
        _rec("zenodo:11459539", "zenodo", SYNAPSE, "Singh, Apoorv", 2024),
        _rec(RG, "rg.rg", SYNAPSE, "Apoorv Singh", 2024),
    ),
]
# Distinct datasets: same normalized title and year, different first authors.
DISTINCT_DATASETS = [
    # One given name, two family names: a last-token key read both as "rui".
    (
        _rec("zenodo:18604146", "zenodo", "Data and Code", "Zhang, rui", 2026),
        _rec(
            "datacite:10.6084/m9.figshare.31046215.v1",
            "figshare",
            "Data and code",
            "Zhe, Rui",
            2026,
        ),
    ),
    # "Lin" is Yang Lin's given name and Hause Lin's family name.
    (
        _rec(
            "datacite:10.6084/m9.figshare.c.4833234.v2",
            "figshare",
            "Data and code",
            "Yang, Lin",
            2020,
        ),
        _rec("datacite:10.17605/osf.io/45gyk", "osf", "Data and Code", "Hause Lin", 2020),
    ),
    # One family name, two people: a family-name key would fold these.
    (
        _rec("datacite:10.5281/zenodo.12776623", "zenodo", "Source Data", "Liu, Ziwei", 2024),
        _rec(
            "datacite:10.6084/m9.figshare.27186855.v1",
            "figshare",
            "Source data",
            "LIU, Shuai",
            2024,
        ),
    ),
]


def test_a_first_author_written_in_either_order_folds_but_a_shared_name_word_does_not() -> None:
    for a, b in SAME_DATASET:
        out = _mirror.collapse_mirrors([a, b])
        assert [r.id for r in out] == [a.id], (a.creators, b.creators)
        assert out[0].mirrors == [Mirror(source=b.source, id=b.id, doi=b.doi)]
    for a, b in DISTINCT_DATASETS:
        assert _mirror.normalize_title(a.title) == _mirror.normalize_title(b.title)
        assert a.year == b.year
        out = _mirror.collapse_mirrors([a, b])
        assert [r.id for r in out] == [a.id, b.id], (a.creators, b.creators)
        assert all(r.mirrors == [] for r in out)


@_live_only
async def test_live_a_reordered_author_folds_and_a_shared_given_name_does_not() -> None:
    """Resolves each record of a real mirror pair and a real distinct pair, checks the
    inputs share the title and year the fingerprint compares, then collapses them."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        zen = await router.resolve(client, "zenodo:11459539")
        rg = await router.resolve(client, RG)
        zhang = await router.resolve(client, "zenodo:18604146")
        zhe = await router.resolve(client, "datacite:10.6084/m9.figshare.31046215.v1")
    for a, b in ((zen, rg), (zhang, zhe)):
        assert _mirror.normalize_title(a.title) == _mirror.normalize_title(b.title)
        assert a.year == b.year and a.source != b.source
    assert (zen.creators[0].name, rg.creators[0].name) == ("Singh, Apoorv", "Apoorv Singh")
    folded = _mirror.collapse_mirrors([zen, rg])
    assert [r.id for r in folded] == ["zenodo:11459539"]
    assert [m.id for m in folded[0].mirrors] == [RG]

    assert (zhang.creators[0].name, zhe.creators[0].name) == ("Zhang, rui", "Zhe, Rui")
    kept = _mirror.collapse_mirrors([zhang, zhe])
    assert [r.id for r in kept] == [zhang.id, zhe.id]
    assert all(r.mirrors == [] for r in kept)


@_live_only
async def test_live_search_folds_the_researchgate_copy_of_a_zenodo_deposit() -> None:
    """A real multi-source search page that holds both copies: with collapse on, the
    ResearchGate copy (DataCite) is a mirror of the Zenodo record, not a second hit."""
    query = "Synapse Open Dataset warehouse robots"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        raw = await router.search_page(
            client, query=query, size=10, sources=["zenodo", "datacite"], collapse_mirrors=False
        )
        assert raw.errors == {}
        assert {"zenodo:11459539", RG} <= {r.id for r in raw.results}, [r.id for r in raw.results]
        page = await router.search_page(
            client, query=query, size=10, sources=["zenodo", "datacite"], collapse_mirrors=True
        )
    by_id = {r.id: r for r in page.results}
    assert RG not in by_id
    assert RG in [m.id for m in by_id["zenodo:11459539"].mirrors]
