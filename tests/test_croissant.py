from data_aggregator_mcp import croissant
from data_aggregator_mcp.models import Creator, DataResource, FileEntry


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
