from data_aggregator_mcp import ro_crate
from data_aggregator_mcp.models import Creator, DataResource, FileEntry


def _resource() -> DataResource:
    return DataResource(
        id="zenodo:1",
        source="zenodo",
        kind="dataset",
        title="Rice genomes",
        description="d",
        doi="10.5281/zenodo.1",
        creators=[Creator(name="A. Author")],
        year=2024,
        license="cc-by-4.0",
        files=[FileEntry(name="a.csv", url="https://x/a.csv", mime="text/csv", size=10)],
    )


def test_render_produces_ro_crate_graph() -> None:
    c = ro_crate.render(_resource())
    assert c["@context"] == "https://w3id.org/ro/crate/1.1/context"
    graph = {e["@id"]: e for e in c["@graph"]}
    assert graph["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    root = graph["./"]
    assert root["@type"] == "Dataset"
    assert root["name"] == "Rice genomes"
    assert {"@id": "https://x/a.csv"} in root["hasPart"]
    assert graph["https://x/a.csv"]["@type"] == "File"
    assert graph["https://x/a.csv"]["encodingFormat"] == "text/csv"
