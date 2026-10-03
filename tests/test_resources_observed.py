"""What the resources module lists and how it builds and reads a record URI (#88 burn-down).

The listing is wire contract: a client shows ``name``, ``title`` and ``description`` and picks
a reader by ``mimeType``, so each listed resource is compared whole against literal values.
URIs are passed as ``str`` (what mcp 2.x hands the handler) and as ``AnyUrl``.
"""

from __future__ import annotations

import os

import pytest
from pydantic import AnyUrl

from data_aggregator_mcp import resources

_CATALOG = {
    "uri": "dataresource://catalog",
    "name": "sources",
    "title": "Data source catalog",
    "description": "The wired data sources and their capabilities (same payload as the "
    "list_sources tool), as JSON.",
    "mime_type": "application/json",
}
_TEMPLATE = {
    "uri_template": "dataresource://record/{id}",
    "name": "record",
    "title": "Resolved data record",
    "description": "Resolve any record by its source-prefixed id (e.g. zenodo:123, "
    "datacite:10.5061/dryad.x, pdb:1abc, a bare Zenodo id, or a DOI) — the same id the "
    "resolve tool accepts, URL-encoded. Returns the full DataResource as JSON.",
    "mime_type": "application/json",
}


def test_the_catalog_resource_is_listed_exactly() -> None:
    assert [r.model_dump(exclude_none=True) for r in resources.static_resources()] == [_CATALOG]


def test_the_record_template_is_listed_exactly() -> None:
    assert [t.model_dump(exclude_none=True) for t in resources.templates()] == [_TEMPLATE]


# Expected values written by hand from RFC 6570 simple expansion of ``{id}``: every character
# outside ALPHA / DIGIT / "-" / "." / "_" / "~" is percent-encoded, the slash included, so a
# client expanding the listed template builds the same URI as ``record_uri``.
_BUILT = [
    ("zenodo:123", "dataresource://record/zenodo%3A123"),
    ("datacite:10.5061/dryad.x", "dataresource://record/datacite%3A10.5061%2Fdryad.x"),
    ("12345", "dataresource://record/12345"),
    ("a b~c_d-e", "dataresource://record/a%20b~c_d-e"),
    (
        "10.1002/(sici)1099-1573(199903)13:2<163::aid-ptr405>3.0.co;2-#",
        "dataresource://record/10.1002%2F%28sici%291099-1573%28199903%2913%3A2%3C163%3A%3A"
        "aid-ptr405%3E3.0.co%3B2-%23",
    ),
]


@pytest.mark.parametrize(("rid", "uri"), _BUILT, ids=[r for r, _ in _BUILT])
def test_record_uri_encodes_the_id_as_template_expansion_does(rid: str, uri: str) -> None:
    assert resources.record_uri(rid) == uri
    assert resources.parse_record_id(uri) == rid
    assert resources.parse_record_id(AnyUrl(uri)) == rid


def test_the_record_id_keeps_its_own_leading_characters() -> None:
    # only the path's leading slash is stripped, never the id's own first characters
    assert resources.parse_record_id("dataresource://record/XX%3A1") == "XX:1"
    assert resources.parse_record_id("dataresource://record/%2Fx") == "/x"
    assert resources.parse_record_id("dataresource://record/x") == "x"


def test_a_record_uri_without_a_path_names_no_record() -> None:
    assert resources.parse_record_id("dataresource://record") is None
    assert resources.parse_record_id("dataresource://record/") is None
    assert resources.parse_record_id("dataresource://record/zenodo%3A1") == "zenodo:1"


@pytest.mark.parametrize(
    "uri",
    ["https://record/x", "other://record/x", "dataresource://other/x", "dataresource://catalog"],
)
def test_only_the_dataresource_record_host_names_a_record(uri: str) -> None:
    assert resources.parse_record_id(uri) is None
    assert resources.parse_record_id(AnyUrl(uri)) is None
    assert resources.parse_record_id("dataresource://record/x") == "x"


@pytest.mark.parametrize(
    "uri",
    ["https://catalog", "other://catalog", "dataresource://record", "dataresource://catalogue"],
)
def test_only_the_dataresource_catalog_host_is_the_catalog(uri: str) -> None:
    assert resources.is_catalog(uri) is False
    assert resources.is_catalog(AnyUrl(uri)) is False
    assert resources.is_catalog("dataresource://catalog") is True
    assert resources.is_catalog(AnyUrl("dataresource://catalog")) is True


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_the_listing_a_client_sees_is_the_listing_pinned_here() -> None:
    # the real server over stdio: what resources/list and resources/templates/list answer
    import sys

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "data_aggregator_mcp"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_resources()
        templates = await session.list_resource_templates()
    assert [r.model_dump(exclude_none=True) for r in listed.resources] == [_CATALOG]
    assert [t.model_dump(exclude_none=True) for t in templates.resource_templates] == [_TEMPLATE]
