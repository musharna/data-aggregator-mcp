from data_aggregator_mcp import croissant
from data_aggregator_mcp.models import Creator, DataResource, FileEntry, Link


def _resource() -> DataResource:
    return DataResource(
        id="datacite:10.5061/dryad.x",
        source="dryad",
        kind="dataset",
        title="Rice genomes",
        description="d",
        doi="10.5061/dryad.x",
        creators=[Creator(name="A. Author")],
        year=2024,
        license="cc-by-4.0",
        files=[
            FileEntry(
                name="a.csv",
                url="https://x/a.csv",
                mime="text/csv",
                size=10,
                checksum="sha256:deadbeef",
            ),
            FileEntry(name="b.bin", url="https://x/b.bin", checksum="md5:abc123"),
        ],
    )


def test_render_produces_file_level_croissant() -> None:
    m = croissant.render(_resource())
    assert m["@type"] == "Dataset"
    assert m["name"] == "Rice genomes"
    assert m["license"] == "cc-by-4.0"
    dist = {f["name"]: f for f in m["distribution"]}
    assert dist["a.csv"]["@type"] == "cr:FileObject"
    assert dist["a.csv"]["contentUrl"] == "https://x/a.csv"
    assert dist["a.csv"]["encodingFormat"] == "text/csv"
    assert dist["a.csv"]["sha256"] == "deadbeef"
    assert dist["b.bin"]["md5"] == "abc123"


# --- Croissant 1.1 upgrade -------------------------------------------------


def test_conforms_to_is_croissant_1_1() -> None:
    m = croissant.render(_resource())
    assert m["conformsTo"] == "http://mlcommons.org/croissant/1.1"


def test_context_carries_dct_prov_odrl_prefixes() -> None:
    ctx = croissant.render(_resource())["@context"]
    assert ctx["dct"] == "http://purl.org/dc/terms/"
    assert ctx["prov"] == "http://www.w3.org/ns/prov#"
    assert ctx["odrl"] == "http://www.w3.org/ns/odrl/2/"
    # schema.org vocab + croissant prefixes unchanged
    assert ctx["@vocab"] == "https://schema.org/"
    assert ctx["cr"] == "http://mlcommons.org/croissant/"
    assert ctx["sc"] == "https://schema.org/"


def test_prov_was_attributed_to_from_creators() -> None:
    r = _resource()
    r.creators = [
        Creator(name="A. Author", orcid="0000-0002-1825-0097"),
        Creator(name="B. Builder"),
    ]
    agents = croissant.render(r)["prov:wasAttributedTo"]
    assert agents[0] == {
        "@type": "Person",
        "@id": "https://orcid.org/0000-0002-1825-0097",
        "name": "A. Author",
    }
    # No ORCID -> Person without @id.
    assert agents[1] == {"@type": "Person", "name": "B. Builder"}


def test_prov_was_attributed_to_omitted_without_creators() -> None:
    r = _resource()
    r.creators = []
    assert "prov:wasAttributedTo" not in croissant.render(r)


def test_prov_was_derived_from_maps_only_derivation_rels() -> None:
    r = _resource()
    r.links = [
        Link(rel="is_derived_from", target_id="10.5061/dryad.parent"),
        Link(rel="is_version_of", target_id="https://example.org/concept"),
    ]
    derived = croissant.render(r)["prov:wasDerivedFrom"]
    assert {"@id": "https://doi.org/10.5061/dryad.parent"} in derived
    assert {"@id": "https://example.org/concept"} in derived


def test_prov_was_derived_from_omitted_for_non_derivation_rels() -> None:
    r = _resource()
    r.links = [
        Link(rel="is_supplement_to", target_id="10.1/paper"),
        Link(rel="cites", target_id="10.2/other"),
        Link(rel="part_of", target_id="10.3/collection"),
    ]
    assert "prov:wasDerivedFrom" not in croissant.render(r)


def test_keywords_from_subjects() -> None:
    r = _resource()
    r.subjects = ["genomics", "rice"]
    assert croissant.render(r)["keywords"] == ["genomics", "rice"]


def test_keywords_omitted_when_empty() -> None:
    r = _resource()
    r.subjects = []
    assert "keywords" not in croissant.render(r)


def test_date_modified_from_last_updated() -> None:
    r = _resource()
    r.last_updated = "2025-01-02T00:00:00Z"
    assert croissant.render(r)["dateModified"] == "2025-01-02T00:00:00Z"


def test_date_modified_omitted_when_none() -> None:
    r = _resource()
    r.last_updated = None
    assert "dateModified" not in croissant.render(r)


def test_publisher_uses_display_name_map() -> None:
    r = _resource()
    r.source = "zenodo"
    assert croissant.render(r)["publisher"] == {
        "@type": "Organization",
        "name": "Zenodo",
    }


def test_publisher_falls_back_to_raw_source() -> None:
    r = _resource()
    r.source = "some-new-repo"
    assert croissant.render(r)["publisher"] == {
        "@type": "Organization",
        "name": "some-new-repo",
    }


def test_cite_as_emitted_for_bibtex() -> None:
    r = _resource()
    r.citation = "@dataset{author2024, title={Rice genomes}}"
    assert croissant.render(r)["citeAs"] == r.citation


def test_cite_as_omitted_for_non_bibtex_citation() -> None:
    r = _resource()
    r.citation = "A. Author (2024). Rice genomes. Dryad."
    assert "citeAs" not in croissant.render(r)


def test_cite_as_omitted_when_no_citation() -> None:
    r = _resource()
    r.citation = None
    assert "citeAs" not in croissant.render(r)


def test_usage_info_is_license_pointer_when_license_set() -> None:
    r = _resource()
    r.license = "https://creativecommons.org/licenses/by/4.0/"
    m = croissant.render(r)
    assert m["usageInfo"] == "https://creativecommons.org/licenses/by/4.0/"


def test_usage_info_omitted_without_license() -> None:
    r = _resource()
    r.license = None
    assert "usageInfo" not in croissant.render(r)


def test_no_odrl_permission_keys_in_b2_output() -> None:
    """B2 emits only a license pointer — no fabricated ODRL policy."""
    r = _resource()
    r.license = "cc-by-4.0"
    m = croissant.render(r)
    flat = repr(m)
    assert "odrl:Offer" not in flat
    assert "odrl:permission" not in flat
    assert "odrl:action" not in flat


def test_structural_validity_against_croissant_1_1_required_keys() -> None:
    """Real-execution check: the rendered object carries the keys an official
    Croissant 1.1 example carries (cross-checked against
    mlcommons/croissant datasets/1.1/zenodo-head-mri/metadata.json,
    fetched 2026-06-10). The mlcroissant validator is not installed in this
    env, so this is the structural fallback the plan specifies."""
    m = croissant.render(_resource())
    for key in ("@context", "@type", "conformsTo", "name", "distribution"):
        assert key in m, f"missing required Croissant 1.1 key: {key}"
    assert m["@type"] == "Dataset"
    assert m["conformsTo"] == "http://mlcommons.org/croissant/1.1"
    # @context must declare every namespace prefix the emitted keys use.
    assert set(m["@context"]) >= {"@vocab", "cr", "sc", "dct", "prov", "odrl"}
