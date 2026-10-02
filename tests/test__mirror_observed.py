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


def test_a_doi_collision_keeps_the_better_copy_and_a_tie_keeps_the_first() -> None:
    first = _r("zenodo:1", doi="10.5281/ZENODO.1")
    same = _r("dataone:2", source="dataone", doi="10.5281/zenodo.1")
    no_doi = _r("zenodo:3")
    assert _mirror.dedup_by_doi([no_doi, first, same]) == [first, no_doi]
    assert _mirror.dedup_by_doi([same, first]) == [same]
    discovery = _r("nasacmr:C1-X", source="nasacmr", doi="10.3334/x")
    datacite = _r("datacite:10.3334/x", source="figshare", doi="10.3334/x")
    native = _r("dataone:doi:10.3334/x", source="dataone", doi="10.3334/x")
    assert _mirror.dedup_by_doi([discovery, datacite]) == [datacite]
    assert _mirror.dedup_by_doi([datacite, discovery]) == [datacite]
    assert _mirror.dedup_by_doi([discovery, datacite, native]) == [native]
    assert [_mirror.fetch_priority(r) for r in (discovery, datacite, native)] == [
        _mirror.DISCOVERY_ONLY_PRIORITY,
        _mirror.DATACITE_PRIORITY,
        _mirror.NATIVE_PRIORITY,
    ]


def test_records_without_a_fingerprint_never_match_on_the_missing_part() -> None:
    """Two records from different sources with the same title but no author (or no
    year) have no fingerprint, so nothing joins them; the same pair with an author and
    a year folds."""
    bare = [
        _r("zenodo:1", title="Atlas", year=2020),
        _r("dryad:2", source="dryad", title="Atlas", year=2020),
    ]
    assert [r.id for r in _mirror.collapse_mirrors(bare)] == ["zenodo:1", "dryad:2"]
    no_year = [
        _r("zenodo:1", title="Atlas", author="Ada Lovelace"),
        _r("dryad:2", source="dryad", title="Atlas", author="Ada Lovelace"),
    ]
    assert [r.id for r in _mirror.collapse_mirrors(no_year)] == ["zenodo:1", "dryad:2"]
    full = [
        _r("zenodo:1", title="Atlas", author="Ada Lovelace", year=2020),
        _r("dryad:2", source="dryad", title="Atlas", author="Ada Lovelace", year=2020),
    ]
    assert [r.id for r in _mirror.collapse_mirrors(full)] == ["zenodo:1"]
