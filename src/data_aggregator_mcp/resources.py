"""MCP resources — addressable, readable records over the resolve pipeline.

Exposes two things to MCP clients as resources (a separate primitive from tools):
  * a single concrete resource, the source catalog, at ``dataresource://catalog``;
  * a template, ``dataresource://record/{id}``, where ``{id}`` is the SAME
    source-prefixed id the resolve tool accepts (zenodo:123, datacite:10.x,
    pdb:1abc, a bare Zenodo id, a DOI), URL-encoded. read_resource decodes it and
    routes through router.resolve.

Custom scheme ``dataresource://`` (NOT ``data://`` — that is the reserved RFC-2397
data-URI scheme clients may special-case). The id is encoded with quote(safe="")
so its colons and slashes round-trip through the URI path.
"""

from __future__ import annotations

from urllib.parse import quote, unquote

from mcp import types
from pydantic import AnyUrl

SCHEME = "dataresource"
CATALOG_URI = "dataresource://catalog"
RECORD_TEMPLATE = "dataresource://record/{id}"
# No character is kept bare beyond the unreserved ones quote() always keeps, as in RFC 6570
# expansion of {id}. A module constant: mutmut's "XXXX" for an inline '' is equivalent
# (X is unreserved), and test_record_uri_encodes_the_id_as_template_expansion_does pins it.
_ID_SAFE = ""


def record_uri(resolve_id: str) -> str:
    return f"{SCHEME}://record/{quote(resolve_id, safe=_ID_SAFE)}"


def _as_url(uri: str | AnyUrl) -> AnyUrl:
    # mcp 2.x hands resource URIs to handlers as plain str (1.x parsed them to AnyUrl);
    # parse here so scheme/host/path checks stay structural, not substring matches.
    return uri if isinstance(uri, AnyUrl) else AnyUrl(uri)


def _names_only_host_and_path(url: AnyUrl) -> bool:
    # A resource here is named by scheme, host and path alone. A query, fragment, user,
    # password or port makes it a different URI, which this server does not serve; reading it
    # as the bare URI resolved a record the caller did not name, and cut an id sent unencoded
    # at its first "#" or "?" (Wiley SICI DOIs end in "#"). An empty "?" or "#" parses as "".
    return (
        url.query is None
        and url.fragment is None
        and url.username is None
        and url.password is None
        and url.port is None
    )


def is_catalog(uri: str | AnyUrl) -> bool:
    # exactly dataresource://catalog — a trailing path (catalog/extra) is NOT the catalog.
    url = _as_url(uri)
    return (
        url.scheme == SCHEME
        and url.host == "catalog"
        and not url.path
        and _names_only_host_and_path(url)
    )


def parse_record_id(uri: str | AnyUrl) -> str | None:
    """Return the decoded resolve-id for a ``dataresource://record/<id>`` URI,
    else None (not a record URI, an empty id, or a URI with more than host and path)."""
    url = _as_url(uri)
    if url.scheme != SCHEME or url.host != "record" or not _names_only_host_and_path(url):
        return None
    raw = (url.path or "").lstrip("/")
    if not raw:
        return None
    return unquote(raw)


def static_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=CATALOG_URI,
            name="sources",
            title="Data source catalog",
            description="The wired data sources and their capabilities (same payload as the "
            "list_sources tool), as JSON.",
            mime_type="application/json",
        )
    ]


def templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            uri_template=RECORD_TEMPLATE,
            name="record",
            title="Resolved data record",
            description="Resolve any record by its source-prefixed id (e.g. zenodo:123, "
            "datacite:10.5061/dryad.x, pdb:1abc, a bare Zenodo id, or a DOI) — the same id "
            "the resolve tool accepts, URL-encoded. Returns the full DataResource as JSON.",
            mime_type="application/json",
        )
    ]
