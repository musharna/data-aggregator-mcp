"""Standing gate: each tool's representative output must validate against the
outputSchema the tool declares. The MCP SDK runs this same validation at
runtime, so a mismatch here is a real client-facing break, not a test nicety.

When a later task adds a DataResource/SearchResult field, populate it in the
representative instance below so the gate exercises the new field too.
"""

from __future__ import annotations

import jsonschema

from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FetchResult,
    FileEntry,
    Link,
    SearchResult,
)


def _validate(instance_model, schema_model) -> None:
    """Dump the model the way MCP serializes it (JSON mode) and validate it
    against the model's own JSON schema."""
    payload = instance_model.model_dump(mode="json")
    jsonschema.validate(payload, schema_model.model_json_schema())


def _sample_resource() -> DataResource:
    return DataResource(
        id="datacite:10.5061/dryad.x",
        source="dryad",
        kind="dataset",
        title="t",
        creators=[Creator(name="A. Author", orcid="0000-0002-1825-0097")],
        year=2024,
        description="d",
        doi="10.5061/dryad.x",
        files=[FileEntry(name="a.csv", url="https://x/a.csv", checksum="md5:abc")],
        links=[Link(rel="is_supplement_to", target_id="datacite:10.1/y")],
    )


def test_dataresource_dump_validates_against_schema() -> None:
    _validate(_sample_resource(), DataResource)


def test_searchresult_dump_validates_against_schema() -> None:
    sr = SearchResult(query="q", total=1, count=1, results=[_sample_resource()])
    _validate(sr, SearchResult)


def test_fetchresult_dump_validates_against_schema() -> None:
    _validate(FetchResult(paths=["/tmp/a"], bytes=1), FetchResult)
