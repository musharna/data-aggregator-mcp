from __future__ import annotations

from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FetchResult,
    FileEntry,
    FundingRef,
    SearchResult,
    _orcid,
    compact,
    normalize_access,
)


def test_creator_and_funding_types():
    c = Creator(name="Ada Lovelace", orcid="0000-0002-1825-0097")
    assert c.name == "Ada Lovelace" and c.orcid == "0000-0002-1825-0097"
    assert Creator(name="No ORCID").orcid is None
    f = FundingRef(funder="NSF", award="ABC-123")
    assert f.funder == "NSF" and f.award == "ABC-123"


def test_orcid_strips_url_and_validates():
    assert _orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert _orcid("0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert _orcid("0000-0002-1825-009X") == "0000-0002-1825-009X"  # checksum X allowed
    assert _orcid("garbage") is None
    assert _orcid(None) is None


def test_compact_preserves_creators_funding_links():
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        creators=[Creator(name="A", orcid="0000-0002-1825-0097")],
        funding=[FundingRef(funder="NSF", award="X")],
        description="d" * 1000,
        files=[],
    )
    c = compact(r)
    assert c.creators == r.creators
    assert c.funding == r.funding


def test_dataresource_defaults_are_empty_collections() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert r.creators == [] and r.files == [] and r.links == []
    assert r.organism == [] and r.doi is None


def test_fileentry_roundtrips() -> None:
    f = FileEntry(name="d.csv", size=10, url="https://x/d.csv", checksum="md5:abc")
    assert f.model_dump()["checksum"] == "md5:abc"


def test_search_result_shape() -> None:
    sr = SearchResult(query="rice", total=2, count=1, results=[], errors={"sra": "timeout"})
    dumped = sr.model_dump()
    assert dumped["errors"] == {"sra": "timeout"}
    assert dumped["next_cursor"] is None


def test_fetch_result_shape() -> None:
    fr = FetchResult(paths=["/tmp/a"], bytes=10, skipped=[])
    assert fr.model_dump() == {"paths": ["/tmp/a"], "bytes": 10, "skipped": [], "resumed": []}


def test_fetch_result_resumed_roundtrip() -> None:
    assert FetchResult(resumed=["a"]).resumed == ["a"]
    assert FetchResult().resumed == []
    assert FetchResult(resumed=["a"]).model_dump()["resumed"] == ["a"]


def test_compact_strips_files_and_truncates_description() -> None:
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        description="x" * 1000,
        files=[FileEntry(name="a.gz", size=1, url="http://e/a.gz")],
    )
    c = compact(r)
    assert c.files == []
    assert c.description is not None and len(c.description) == 500
    # original is untouched (model_copy, not mutation)
    assert len(r.files) == 1


def test_compact_handles_none_description() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert compact(r).description is None


def test_taxon_and_taxa_field() -> None:
    from data_aggregator_mcp.models import DataResource, Taxon

    t = Taxon(taxid=99112, name="Phelipanche aegyptiaca")
    assert t.taxid == 99112 and t.name == "Phelipanche aegyptiaca"
    r = DataResource(id="geo:GSE1", source="geo", kind="study", title="t")
    assert r.taxa == []  # additive, defaults empty
    r2 = r.model_copy(update={"taxa": [t]})
    assert r2.model_dump()["taxa"] == [{"taxid": 99112, "name": "Phelipanche aegyptiaca"}]


def test_taxon_expansion_on_search_result() -> None:
    from data_aggregator_mcp.models import SearchResult, TaxonExpansion

    sr = SearchResult(query="q", total=0, count=0)
    assert sr.taxon_expansion is None  # additive, defaults None
    sr2 = SearchResult(
        query="q",
        total=0,
        count=0,
        taxon_expansion=TaxonExpansion(
            input="Orobanche aegyptiaca",
            taxid=99112,
            canonical_name="Phelipanche aegyptiaca",
            synonyms=["Orobanche aegyptiaca"],
        ),
    )
    assert sr2.model_dump()["taxon_expansion"]["taxid"] == 99112


def test_access_and_citation_default_none() -> None:
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert r.access is None
    assert r.citation is None
    dumped = r.model_dump()
    assert "access" in dumped and "citation" in dumped


def test_normalize_access_maps_known_tokens() -> None:
    assert normalize_access("open") == "open"
    assert normalize_access("OPEN") == "open"
    assert normalize_access("embargo") == "embargoed"
    assert normalize_access("embargoed") == "embargoed"
    assert normalize_access("restricted") == "restricted"
    assert normalize_access("closed") == "closed"
    assert normalize_access("weird-value") == "unknown"
    assert normalize_access(None) is None
    assert normalize_access("") is None


def test_compact_preserves_access_drops_files() -> None:
    r = DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="t",
        access="open",
        description="d",
    )
    c = compact(r)
    assert c.access == "open"  # small scalar — useful in search results
    assert c.files == []


def test_dataresource_identifiers_defaults_empty() -> None:
    r = DataResource(id="pubmed:1", source="pubmed", kind="publication", title="t")
    assert r.identifiers == {}


def test_fileentry_source_optional() -> None:
    assert FileEntry(name="y.xml").source is None
    assert FileEntry(name="x.xml", source="europepmc").source == "europepmc"


def test_compact_preserves_identifiers_and_drops_files() -> None:
    r = DataResource(
        id="pubmed:1",
        source="pubmed",
        kind="publication",
        title="t",
        identifiers={"pmid": "1", "pmcid": "PMC9"},
        files=[FileEntry(name="f.xml", url="http://x/f")],
    )
    c = compact(r)
    assert c.identifiers == {"pmid": "1", "pmcid": "PMC9"}
    assert c.files == []


def test_orcid_accepts_lowercase_x_checksum():
    from data_aggregator_mcp.models import _orcid

    # ORCID checksum may arrive lowercase; canonicalize to uppercase X.
    assert _orcid("0000-0002-1825-009x") == "0000-0002-1825-009X"
    assert _orcid("https://orcid.org/0000-0002-1825-009x") == "0000-0002-1825-009X"


def test_metrics_default_none_and_populated_roundtrip() -> None:
    from data_aggregator_mcp.models import DataResource, Metrics

    bare = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="t")
    assert bare.metrics is None

    m = DataResource(
        id="datacite:10.x/y",
        source="dryad",
        kind="dataset",
        title="t",
        metrics=Metrics(citations=3, views=100, downloads=42, likes=None),
    )
    dumped = m.model_dump()
    assert dumped["metrics"]["citations"] == 3
    assert dumped["metrics"]["likes"] is None
