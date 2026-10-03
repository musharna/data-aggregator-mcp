"""Which URIs name a resource here, and which only look like one (#88 mutation burn-down).

A resource is named by scheme, host and path alone. Anything else in the URI makes it a
different URI, which this server does not serve. Reading it as the bare URI resolved a record
the caller did not name (``dataresource://record/pdb%3A1bg2?v=2`` read pdb:1BG2 over stdio), and
cut an id sent unencoded at its first ``#`` or ``?``: Wiley SICI DOIs end in ``#`` (7 of 500
sampled 1996-2000 Wiley DOIs), and ``…;2-#`` was resolved as ``…;2-``.

URIs are passed as ``str`` (what mcp 2.x hands the handler) and as ``AnyUrl``.
"""

from __future__ import annotations

import os

import pytest
from pydantic import AnyUrl

from data_aggregator_mcp import resources

_EXTRA = ["?v=2", "?", "#x", "#"]
_USERINFO = ["u@", ":p@", "u:p@"]
_SICI_DOI = "10.1002/(sici)1099-1573(199903)13:2<163::aid-ptr405>3.0.co;2-#"


@pytest.mark.parametrize("extra", _EXTRA)
def test_a_record_uri_with_a_query_or_fragment_names_no_record(extra: str) -> None:
    uri = resources.record_uri("pdb:1bg2")
    assert resources.parse_record_id(uri) == "pdb:1bg2"
    assert resources.parse_record_id(AnyUrl(uri)) == "pdb:1bg2"
    assert resources.parse_record_id(uri + extra) is None
    assert resources.parse_record_id(AnyUrl(uri + extra)) is None


def test_an_unencoded_id_is_refused_rather_than_cut_at_its_hash() -> None:
    assert resources.parse_record_id(resources.record_uri(_SICI_DOI)) == _SICI_DOI
    assert resources.parse_record_id("dataresource://record/" + _SICI_DOI) is None


@pytest.mark.parametrize("userinfo", _USERINFO)
def test_a_record_uri_with_userinfo_names_no_record(userinfo: str) -> None:
    assert resources.parse_record_id("dataresource://record/zenodo%3A1") == "zenodo:1"
    assert resources.parse_record_id(f"dataresource://{userinfo}record/zenodo%3A1") is None


def test_a_record_uri_with_a_port_names_no_record() -> None:
    assert resources.parse_record_id("dataresource://record/zenodo%3A1") == "zenodo:1"
    assert resources.parse_record_id("dataresource://record:80/zenodo%3A1") is None


@pytest.mark.parametrize("extra", _EXTRA)
def test_the_catalog_uri_with_a_query_or_fragment_is_not_the_catalog(extra: str) -> None:
    assert resources.is_catalog(resources.CATALOG_URI) is True
    assert resources.is_catalog(AnyUrl(resources.CATALOG_URI)) is True
    assert resources.is_catalog(resources.CATALOG_URI + extra) is False
    assert resources.is_catalog(AnyUrl(resources.CATALOG_URI + extra)) is False


@pytest.mark.parametrize("authority", [*(u + "catalog" for u in _USERINFO), "catalog:80"])
def test_the_catalog_uri_with_userinfo_or_a_port_is_not_the_catalog(authority: str) -> None:
    assert resources.is_catalog("dataresource://catalog") is True
    assert resources.is_catalog(f"dataresource://{authority}") is False


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
async def test_live_a_uri_with_more_than_host_and_path_is_refused_over_stdio() -> None:
    # The real server over a stdio session, beside the same record read through its bare URI.
    # On the old code each refused URI below answered pdb:1BG2 or the catalog, and the
    # unencoded SICI DOI was looked up as "...;2-".
    import json
    import sys

    from mcp import ClientSession, MCPError
    from mcp.client.stdio import StdioServerParameters, stdio_client

    bare = resources.record_uri("pdb:1bg2")
    refused = [
        bare + "?v=2",
        bare + "#x",
        bare.replace("://", "://u:p@", 1),
        bare.replace("record/", "record:80/", 1),
        resources.CATALOG_URI + "?v=2",
        "dataresource://record/" + _SICI_DOI,
    ]
    errors = {}
    # the whole environment, PYTHONPATH included, so the server runs the code this test imports
    # (stdio_client passes only a short allow-list of variables by default)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "data_aggregator_mcp"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        record = await session.read_resource(bare)
        for uri in refused:
            with pytest.raises(MCPError) as caught:
                await session.read_resource(uri)
            errors[uri] = str(caught.value)
    rec = json.loads(record.contents[0].text)
    assert rec["id"].lower() == "pdb:1bg2" and rec["source"] == "pdb"
    assert errors == {u: f"not a readable data-aggregator resource: {u}" for u in refused}
