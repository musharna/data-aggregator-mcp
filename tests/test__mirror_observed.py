"""Exact behaviour of DOI dedup and mirror collapse: which record survives, which
records fold, and in what order the output and the mirrors come back."""

from __future__ import annotations

import itertools

from data_aggregator_mcp import _mirror
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Mirror


def _r(
    id_: str,
    *,
    source: str = "zenodo",
    doi: str | None = None,
    sums: tuple[str, ...] = (),
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
) -> DataResource:
    return DataResource(
        id=id_,
        source=source,
        kind="dataset",
        title=title or f"title of {id_}",
        creators=[Creator(name=author)] if author else [],
        year=year,
        doi=doi,
        files=[FileEntry(name=f"f{n}", checksum=c) for n, c in enumerate(sums)],
    )


def _chain() -> list[DataResource]:
    """Five records joined only through a chain of shared checksums that one merge pass
    cannot close: c meets a only through d and b only through e."""
    return [
        _r("zenodo:a", sums=("md5:x",)),
        _r("zenodo:b", doi="10.1/b", sums=("md5:y",)),
        _r("zenodo:c", doi="10.1/c", sums=("md5:z", "md5:w")),
        _r("zenodo:d", doi="10.1/d", sums=("md5:x", "md5:z")),
        _r("zenodo:e", doi="10.1/e", sums=("md5:y", "md5:w")),
    ]


def test_the_survivor_is_the_earliest_best_ranked_record_and_mirrors_keep_input_order() -> None:
    """a has no DOI, so the survivor is the first DOI-bearing record, b. Merging in
    passes put d ahead of b in the group, and d was chosen."""
    out = _mirror.collapse_mirrors(_chain())
    assert [r.id for r in out] == ["zenodo:b"]
    assert out[0].mirrors == [
        Mirror(source="zenodo", id=i, doi=d)
        for i, d in [
            ("zenodo:a", None),
            ("zenodo:c", "10.1/c"),
            ("zenodo:d", "10.1/d"),
            ("zenodo:e", "10.1/e"),
        ]
    ]


def test_the_groups_do_not_depend_on_the_order_records_arrive_in() -> None:
    recs = [
        *_chain(),
        _r("zenodo:f", title="Atlas", author="Ada Lovelace", year=2020),
        _r("dryad:g", source="dryad", title="Atlas", author="Lovelace, Ada", year=2020),
        _r("zenodo:h", title="Atlas", author="Ada Lovelace", year=2020),
        _r("zenodo:i", title="Atlas", author="Ada Lovelace", year=2021),
    ]
    expected = {
        frozenset({"zenodo:a", "zenodo:b", "zenodo:c", "zenodo:d", "zenodo:e"}),
        frozenset({"zenodo:f", "dryad:g", "zenodo:h"}),
        frozenset({"zenodo:i"}),
    }
    for perm in itertools.islice(itertools.permutations(recs), 0, None, 997):
        out = _mirror.collapse_mirrors(list(perm))
        assert {frozenset([r.id, *(m.id for m in r.mirrors)]) for r in out} == expected
