import os

import httpx
import pytest

from data_aggregator_mcp import openneuro

_DOI = "10.18112/openneuro.ds000001.v1.0.0"
_GQL = {
    "data": {
        "snapshot": {
            "files": [
                {
                    "filename": "README",
                    "size": 1175,
                    "directory": False,
                    "urls": [
                        "https://openneuro.org/crn/datasets/ds000001/objects/abc?filename=README"
                    ],
                },
                {"filename": "sub-01", "size": None, "directory": True, "urls": []},
                {
                    "filename": "sub-01/anat/sub-01_T1w.nii.gz",
                    "size": 9,
                    "directory": False,
                    "urls": ["https://s3.amazonaws.com/openneuro.org/ds000001/sub-01/anat/t1"],
                },
                {
                    "filename": "dataset_description.json",
                    "size": 615,
                    "directory": False,
                    "urls": [
                        "https://openneuro.org/crn/datasets/ds000001/objects/def?filename=dataset_description.json"
                    ],
                },
            ]
        }
    }
}


@pytest.mark.asyncio
async def test_files_builds_manifest_from_doi():
    async def handler(request):
        assert request.url.host == "openneuro.org" and request.url.path.endswith("/graphql")
        import json

        parsed = json.loads(request.content.decode())
        # Must use GraphQL variables — ds/tag must be in "variables", not inlined
        assert "variables" in parsed, "request body must contain a 'variables' key"
        assert parsed["variables"].get("ds") == "ds000001"
        assert parsed["variables"].get("tag") == "1.0.0"
        # The query string must use $ds / $tag variable placeholders
        assert "$ds" in parsed["query"] and "$tag" in parsed["query"]
        return httpx.Response(200, json=_GQL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await openneuro.files(c, _DOI)
    assert [f.name for f in files] == [  # directory entry itself dropped, its file kept
        "README",
        "sub-01/anat/sub-01_T1w.nii.gz",
        "dataset_description.json",
    ]
    assert files[0].url.startswith("https://openneuro.org/crn/datasets/ds000001/objects/")
    assert files[0].source == "openneuro" and files[0].size == 1175


# A BIDS snapshot as OpenNeuro stores it: nested directories under each subject.
_TREE = {
    "README": 1175,
    "dataset_description.json": 615,
    "sub-01/anat/sub-01_T1w.nii.gz": 100,
    "sub-01/func/sub-01_run-01_bold.nii.gz": 200,
    "sub-01/func/sub-01_run-01_events.tsv": 30,
    "sub-02/anat/sub-02_T1w.nii.gz": 101,
}


def _entry(filename, size=None, directory=False):
    urls = [] if directory else [f"https://openneuro.org/crn/objects/x?filename={filename}"]
    return {"filename": filename, "size": size, "directory": directory, "urls": urls}


def _fake_snapshot_server(request):
    """Answer like the real API: files(recursive:true) returns every file with its path
    from the dataset root; plain files returns the top level, directories unexpanded."""
    import json

    query = "".join(json.loads(request.content.decode())["query"].split())
    if "files(recursive:true)" in query:
        listing = [_entry(name, size) for name, size in _TREE.items()]
    else:
        tops = {name.split("/", 1)[0] for name in _TREE}
        listing = [_entry(t, _TREE.get(t), directory=t not in _TREE) for t in sorted(tops)]
    return httpx.Response(200, json={"data": {"snapshot": {"files": listing}}})


@pytest.mark.asyncio
async def test_files_lists_the_whole_snapshot_tree():
    """A-H6: only the top level was listed (ds000001: 6 metadata files, sub-01..sub-16
    skipped). The manifest is every file in the tree, each keeping its relative path."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_fake_snapshot_server)) as c:
        files = await openneuro.files(c, _DOI)
    assert sorted(f.name for f in files) == sorted(_TREE)
    bold = next(f for f in files if f.name == "sub-01/func/sub-01_run-01_bold.nii.gz")
    assert bold.size == 200 and bold.url


@pytest.mark.asyncio
async def test_files_directory_without_contents_raises():
    """A recursive listing that still carries a directory with nothing under it has not
    been expanded: raise naming OpenNeuro and the snapshot. Positive control: a
    recursive listing whose directory entries sit beside their files succeeds."""
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    ok = [_entry("sub-01", directory=True), _entry("sub-01/anat/a.nii.gz", 5), _entry("README", 1)]
    bad = [_entry("sub-01", directory=True), _entry("README", 1)]
    for listing, expect_ok in ((ok, True), (bad, False)):
        body = {"data": {"snapshot": {"files": listing}}}
        transport = httpx.MockTransport(lambda r, body=body: httpx.Response(200, json=body))
        async with httpx.AsyncClient(transport=transport) as c:
            if expect_ok:
                files = await openneuro.files(c, _DOI)
                assert [f.name for f in files] == ["sub-01/anat/a.nii.gz", "README"]
            else:
                with pytest.raises(UpstreamUnavailableError, match=r"OpenNeuro.*ds000001.*sub-01"):
                    await openneuro.files(c, _DOI)


@pytest.mark.asyncio
async def test_files_non_openneuro_doi_returns_empty():
    # A non-OpenNeuro DOI must short-circuit BEFORE any network call: the
    # transport raises so the test fails loudly if files() ever reaches out.
    def _never(request):
        raise AssertionError("files() made a network call for an unparseable DOI")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_never)) as c:
        assert await openneuro.files(c, "10.5061/dryad.xyz") == []  # no parse → no network → []


@pytest.mark.asyncio
async def test_files_empty_snapshot_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"snapshot": None}})
        )
    ) as c:
        assert await openneuro.files(c, _DOI) == []


def test_wired_into_datacite_and_fetchable():
    from data_aggregator_mcp import datacite, server

    assert datacite._FILE_RESOLVERS.get("openneuro") is openneuro.files
    assert datacite._source_for_client("sul.openneuro") == "openneuro"
    assert "openneuro" in server._DATACITE_FETCHABLE


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_snapshot_tree_is_complete():
    """ds000001 v1.0.0 is immutable: 136 files, sub-01..sub-16 each with anat/ and func/.
    OpenNeuro's own summary counts 133 (it leaves out the 3 git/datalad dotfiles)."""
    async with httpx.AsyncClient(timeout=120) as c:
        fs = await openneuro.files(c, "10.18112/openneuro.ds000001.v1.0.0")
    names = {f.name for f in fs}
    assert len(fs) == len(names) == 136
    assert len({n for n in names if not n.startswith(".")}) == 133
    for i in range(1, 17):
        assert any(n.startswith(f"sub-{i:02d}/anat/") for n in names)
        assert any(n.startswith(f"sub-{i:02d}/func/") for n in names)


@_live_only
@pytest.mark.asyncio
async def test_live_files_for_known_doi():
    async with httpx.AsyncClient(timeout=60) as c:
        fs = await openneuro.files(c, "10.18112/openneuro.ds000001.v1.0.0")
        assert fs and all(f.source == "openneuro" and f.url for f in fs)


def test_snapshot_query_declares_datasetid_as_id_not_string():
    """OpenNeuro's schema is snapshot(datasetId: ID!). GraphQL rejects the whole document
    when a variable's declared type mismatches the argument position, so declaring
    $ds:String! made every snapshot lookup return HTTP 400 and no dataset ever got a
    manifest — a total, silent loss of OpenNeuro file listings."""
    assert "$ds:ID!" in openneuro._QUERY
    assert "$ds:String!" not in openneuro._QUERY


@pytest.mark.asyncio
async def test_files_graphql_errors_raise_not_empty(monkeypatch):
    """Audit 2026-09-22 H1: GraphQL reports failures as HTTP 200 with ``errors[]`` and
    ``data: null`` (the ID!/String! mismatch was exactly this). It read as an empty
    manifest, so the resolve looked fine and a fetch "succeeded" with zero files."""
    from data_aggregator_mcp import _http
    from data_aggregator_mcp.errors import UpstreamUnavailableError

    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    err = {
        "errors": [{"message": "Variable $ds of type String! used in position expecting ID!"}],
        "data": None,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=err))
    ) as c:
        with pytest.raises(UpstreamUnavailableError, match="expecting ID!"):
            await openneuro.files(c, "10.18112/openneuro.ds000001.v1.0.0")
    ok = {
        "data": {
            "snapshot": {
                "files": [
                    {"filename": "a.tsv", "size": 3, "directory": False, "urls": ["https://x/a"]}
                ]
            }
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ok))
    ) as c:
        files = await openneuro.files(c, "10.18112/openneuro.ds000001.v1.0.0")  # control
    assert [f.name for f in files] == ["a.tsv"]
