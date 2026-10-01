"""OpenNeuro file-manifest resolver — BIDS neuroimaging datasets.

OpenNeuro discovery rides the DataCite firehose (its 10.18112/openneuro.* DOIs,
client `sul.openneuro`, are indexed there), so there is no native search adapter.
This module is the bespoke fetch backend: datacite.resolve() dispatches an
OpenNeuro-sourced DOI here to populate files[]. The dataset id + version are
parsed from the DOI (10.18112/openneuro.<dsID>.v<tag>) and the snapshot's whole
file tree is fetched in ONE GraphQL request, ``files(recursive: true)``: every file
comes back with its path from the dataset root ("sub-01/anat/sub-01_T1w.nii.gz"),
which fetch keeps as the relative directory. The top-level-only listing this
replaced dropped every subject (ds000001: 6 of 136 files). Files are unverified
(no checksum).
"""

from __future__ import annotations

import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

GRAPHQL = "https://openneuro.org/crn/graphql"
# 10.18112/openneuro.ds000001.v1.0.0 → ("ds000001", "1.0.0")
_DOI_RE = re.compile(r"openneuro\.(ds\d+)\.v([\w.]+)", re.IGNORECASE)
# datasetId is ID! in OpenNeuro's schema, not String!. GraphQL rejects the whole
# document when a variable's declared type does not match the argument's ("Variable
# $ds of type String! used in position expecting type ID!"), so declaring it String!
# meant every snapshot lookup returned HTTP 400 and no dataset ever got a manifest.
# ``recursive:true`` walks the tree server-side; without it only the top level returns.
_QUERY = (
    "query($ds:ID!,$tag:String!){snapshot(datasetId:$ds,tag:$tag)"
    "{files(recursive:true){filename size directory urls}}}"
)
MAX_RETRIES = 2


def _is_file(f: object) -> bool:
    """A listed file typed as the schema types it: ``filename`` a string, ``urls`` null
    or a list of strings."""
    if not (isinstance(f, dict) and isinstance(f.get("filename"), str)):
        return False
    urls = f.get("urls")
    return urls is None or (isinstance(urls, list) and all(isinstance(u, str) for u in urls))


def _check_snapshot(data: dict) -> None:
    """``snapshot`` is null, or an object whose ``files`` is null or a list of files
    (``_is_file``). A null file would shorten the manifest, so it is off contract too."""
    snapshot = data.get("snapshot")
    listing = snapshot.get("files") if isinstance(snapshot, dict) else None
    if not (
        ("snapshot" in data and snapshot is None)
        or (
            isinstance(snapshot, dict)
            and (listing is None or (isinstance(listing, list) and all(map(_is_file, listing))))
        )
    ):
        raise _http.UpstreamEnvelopeError(f"no snapshot file list in {data!r:.200}")


def _parse(doi: str) -> tuple[str, str] | None:
    m = _DOI_RE.search(doi)
    return (m.group(1), m.group(2)) if m else None


async def files(client: httpx.AsyncClient, doi: str) -> list[FileEntry]:
    parsed = _parse(doi)
    if parsed is None:
        return []
    ds, tag = parsed
    # A snapshot OpenNeuro does not have comes back as GraphQL errors ("Not Found",
    # "Dataset ds999999 does not exist.") beside a null snapshot, so it raises naming
    # the snapshot. A real HTTP 404 means the endpoint itself moved: no
    # not_found_returns, it fails loud.
    data = await _http.graphql(
        client,
        GRAPHQL,
        _QUERY,
        service=f"OpenNeuro snapshot {ds}@{tag}",
        variables={"ds": ds, "tag": tag},
        max_retries=MAX_RETRIES,
        check=_check_snapshot,
    )
    listing = (data["snapshot"] or {}).get("files") or []
    # Every directory that holds a listed file, at any depth.
    parents: set[str] = set()
    for f in listing:
        if not f.get("directory"):
            name = f["filename"]
            parents.update(name[:i] for i, ch in enumerate(name) if ch == "/")
    # A directory entry is only redundant if its files are listed too. One with nothing
    # under it means the tree came back unexpanded, and dropping it would silently
    # truncate the manifest (git/datalad snapshots cannot hold empty directories).
    # OpenNeuro spells a directory without a trailing slash ("sub-01").
    for f in listing:
        if f.get("directory") and f["filename"] not in parents:
            raise UpstreamUnavailableError(
                f"OpenNeuro snapshot {ds}@{tag}: directory {f['filename']!r} came back "
                "without its files; refusing to return a partial manifest"
            )
    # A file listed without a download URL stays in the manifest (url None; fetch
    # reports it skipped) rather than vanishing from it.
    return [
        FileEntry(
            name=f["filename"],
            size=f.get("size"),
            url=(f.get("urls") or [None])[0],
            source="openneuro",
        )
        for f in listing
        if not f.get("directory")
    ]
