"""What PRIDE answers, as probed live, and how a malformed answer is read.

Probed 2026-10-02 against ``https://www.ebi.ac.uk/pride/ws/archive/v3``:

- ``/projects/{acc}/files/count`` is a bare JSON int. An accession PRIDE does not
  have (``PXD999999``, ``foo``, lower-case ``pxd000001``) answers ``200 0``.
- ``/projects/{acc}/files?pageSize=100&page=N`` is a JSON list; a page past the end
  answers ``200 []``. Over 40 random OmicsDI-listed PRIDE projects plus PXD012062
  (605 files) and PXD026648 (235), every row had a non-empty string ``fileName``, an
  int ``fileSizeBytes``, an empty ``checksum`` and two ``publicFileLocations`` (FTP and
  Aspera), and every page walk returned ``count`` distinct files.
"""

import copy
import os

import httpx
import pytest

from data_aggregator_mcp import _http, pride
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

# PXD000001's first file row, verbatim (2026-10-02).
ROW = {
    "projectAccessions": ["PXD000001"],
    "accession": "5bda360133398f66021c8889e01dce921cb51300c7269e1f2b0f20368ab20af6",
    "fileCategory": {
        "@type": "CvParam",
        "cvLabel": "PRIDE",
        "accession": "PRIDE:0000410",
        "name": "Other type file URI",
        "value": "OTHER",
    },
    "checksum": "",
    "publicFileLocations": [
        {
            "@type": "CvParam",
            "cvLabel": "PRIDE",
            "accession": "PRIDE:0000469",
            "name": "FTP Protocol",
            "value": "ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        },
        {
            "@type": "CvParam",
            "cvLabel": "PRIDE",
            "accession": "PRIDE:0000468",
            "name": "Aspera Protocol",
            "value": "prd_ascp@fasp.ebi.ac.uk:pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        },
    ],
    "fileSizeBytes": 497985,
    "fileName": "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
    "compress": False,
    "submissionDate": "2012-03-13T00:00:00.000+00:00",
    "publicationDate": "2012-03-07T00:00:00.000+00:00",
    "updatedDate": "2017-06-20T14:57:06.000+00:00",
    "additionalAttributes": [],
    "totalDownloads": 701,
}

ROW_FILE = FileEntry(
    name="PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
    url="https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
    size=497985,
    checksum=None,
    source="pride",
)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def _now(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _now)
    monkeypatch.setattr(_http._ratelimit, "acquire", _now)


def _serving(count, rows) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files/count"):
            return httpx.Response(200, json=count)
        return httpx.Response(200, json=rows)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _files(count, rows):
    async with _serving(count, rows) as c:
        return await pride.files(c, "PXD000001")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [True, -1], ids=["true", "negative"])
async def test_a_count_that_is_not_a_file_count_is_upstream_trouble(count):
    """``true`` is an int to ``isinstance``, so it was read as one file; a negative count
    was reported as a short page walk. Positive controls: a real count reads, and the
    live answer for an unknown project (``0``) is no files."""
    assert await _files(1, [ROW]) == [ROW_FILE]
    assert await _files(0, []) == []
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] PRIDE file count returned an unparseable 200 "
        r"body after 2 tries: UpstreamEnvelopeError\('no PRIDE file count in ",
    ):
        await _files(count, [ROW])


_NO_NAME = {k: v for k, v in ROW.items() if k != "fileName"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        _NO_NAME,
        {**ROW, "fileName": ""},
        {**ROW, "fileName": None},
        {**ROW, "fileSizeBytes": "497985"},
        {**ROW, "fileSizeBytes": True},
        {**ROW, "publicFileLocations": ROW["publicFileLocations"][0]["value"]},
        {**ROW, "publicFileLocations": [ROW["publicFileLocations"][0]["value"]]},
        {**ROW, "publicFileLocations": [{"value": 7}]},
    ],
    ids=[
        "row-is-a-string",
        "no-name",
        "empty-name",
        "null-name",
        "size-as-string",
        "size-as-true",
        "locations-a-string",
        "location-a-string",
        "location-value-an-int",
    ],
)
async def test_a_malformed_file_row_is_upstream_trouble(row):
    """A row the reader cannot read whole was an ``AttributeError``/``TypeError``/pydantic
    error, a file named ``""``, or a size coerced from a string or ``true``. Positive
    control: the live row reads whole beside it."""
    assert await _files(1, [ROW]) == [ROW_FILE]
    with pytest.raises(
        UpstreamUnavailableError,
        match=r"^\[UpstreamUnavailableError\] PRIDE files returned an unparseable 200 body "
        r"after 2 tries: UpstreamEnvelopeError\(['\"]no PRIDE file list in \[",
    ):
        await _files(2, [ROW, row])


_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(base, path, value):
    if not path:
        return value
    rec = copy.deepcopy(base)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


@pytest.mark.asyncio
async def test_no_wrong_typed_field_escapes_as_a_bare_error():
    """The row itself and every field of the live row, set to every JSON type: the page
    is refused as upstream trouble, or the row reads cleanly. A field the reader starts
    using without the check fails here."""
    assert await _files(1, [ROW]) == [ROW_FILE]  # positive control
    escaped = []
    for path in _paths(ROW):
        for value in _WRONG:
            try:
                await _files(1, [_with(ROW, path, value)])
            except UpstreamUnavailableError as exc:
                if "PRIDE files returned an unparseable 200 body" not in str(exc):
                    escaped.append((path, value, str(exc)))
            except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                escaped.append((path, value, type(exc).__name__))
    assert escaped == []


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_an_unknown_project_counts_zero_files(monkeypatch):
    """The mocked "no files" above is PRIDE's live answer for a project it does not
    have; the live row passes the check and reads as ``ROW_FILE``."""
    monkeypatch.undo()  # real pacing for the real upstream
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        raw = await c.get(
            pride.V3_COUNT.format(acc="PXD999999"), headers={"Accept": "application/json"}
        )
        assert (raw.status_code, raw.json()) == (200, 0)
        assert await pride.files(c, "PXD999999") == []
        first = await pride.files(c, "PXD000001")
    assert ROW_FILE in first
