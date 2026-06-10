import os

import pytest
from pydantic import AnyUrl

from data_aggregator_mcp import resources


def test_record_uri_round_trips_simple_and_doi_ids():
    for rid in ["zenodo:123", "datacite:10.5061/dryad.x", "pdb:1abc", "12345"]:
        uri = resources.record_uri(rid)
        assert uri.startswith("dataresource://record/")
        assert resources.parse_record_id(AnyUrl(uri)) == rid  # colons + slashes survive


def test_is_catalog_only_for_catalog_uri():
    assert resources.is_catalog(AnyUrl(resources.CATALOG_URI)) is True
    assert resources.is_catalog(AnyUrl("dataresource://record/zenodo%3A1")) is False
    assert resources.is_catalog(AnyUrl("https://example.com")) is False
    assert (
        resources.is_catalog(AnyUrl("dataresource://catalog/extra")) is False
    )  # trailing path ≠ catalog


def test_parse_record_id_rejects_non_record_uris():
    assert resources.parse_record_id(AnyUrl(resources.CATALOG_URI)) is None
    assert resources.parse_record_id(AnyUrl("https://example.com/record/x")) is None
    assert resources.parse_record_id(AnyUrl("dataresource://record/")) is None  # empty id


def test_static_resources_lists_the_catalog():
    res = resources.static_resources()
    assert len(res) == 1
    assert str(res[0].uri) == resources.CATALOG_URI and res[0].mimeType == "application/json"


def test_templates_expose_the_record_template():
    tmpls = resources.templates()
    assert len(tmpls) == 1
    assert tmpls[0].uriTemplate == "dataresource://record/{id}"
    assert tmpls[0].mimeType == "application/json"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_read_resource_resolves_real_record() -> None:
    import json

    from data_aggregator_mcp import server

    contents = await server._read_resource(AnyUrl(resources.record_uri("pdb:1bg2")))
    rec = json.loads(list(contents)[0].content)
    assert rec["id"] == "pdb:1bg2" and rec["source"] == "pdb"
