"""Each Dataverse answer beside how the file lister must read it.

Probed live 2026-10-02 (anonymous, native API):
- Dataverse mints a DOI per file as well as per dataset: 85 of 100 random Harvard DOIs
  that DataCite types as ``Dataset`` are file DOIs (``10.7910/DVN/BQFSOS/LVIXTT``). The
  dataset endpoint answers them ``404`` (Harvard's body wrongly says the persistentId
  parameter is missing; DataverseNO answers an HTML 404 page), so a DataCite resolve of
  one failed with ``NotFoundError``; ``/api/files/:persistentId/`` answers with the file.
- A dataset Harvard no longer holds (``10.7910/DVN/SJNBKL``, still findable at DataCite)
  is ``404`` on both endpoints. A deaccessioned one (``10.7910/DVN/P7UKWR``) is ``200``
  with no ``latestVersion``.
- A file under embargo (``10.7910/DVN/18ERYS``, ``Study2_populism.tab``, available
  2028-01-31) has ``restricted: false`` and an ``embargo`` block; its download answers
  403. Dataverse's ``FileUtil.isActivelyEmbargoed`` (dateAvailable after today) and
  ``isRetentionExpired`` (dateUnavailable before today) decide access.
- DataverseNL (DANS) checksums with SHA-1: its files carry ``checksum`` and no ``md5``
  (135 of 135 sampled), so they were listed with no checksum.
- 750 of 926 public files sampled sit in a folder (``directoryLabel``); KU Leuven RDR's
  ``10.48804/OJUPQB`` holds ``Arithmetic.m`` in two folders, and 56 of the 926 shared a
  bare name with another file of the same dataset.
- 81 answers (40 dataset, 41 file; Harvard, Borealis, DataverseNO, DataverseNL, KU
  Leuven RDR, bonndata, CIMMYT) holding 980 files: every field read came back at the
  type below or absent, so the checks refuse 0 of 81.
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import date, timedelta

import httpx
import pytest

from data_aggregator_mcp import _http, dataverse
from data_aggregator_mcp.errors import UpstreamUnavailableError
from data_aggregator_mcp.models import FileEntry

LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")

_H = "https://dataverse.harvard.edu"
_DL = f"{_H}/api/access/datafile"


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    async def _ns(*_a, **_k):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _ns)
    monkeypatch.setattr(_http._ratelimit, "acquire", _ns)  # the field walk sends ~600


@pytest.fixture(autouse=True)
def no_env_base(monkeypatch):
    monkeypatch.delenv("DATAVERSE_BASE_URL", raising=False)


# Verbatim from 10.7910/DVN/18ERYS, trimmed to the fields Dataverse always sends.
_README = {
    "label": "README.txt",
    "restricted": False,
    "version": 1,
    "datasetVersionId": 747429,
    "dataFile": {
        "id": 14250294,
        "persistentId": "",
        "filename": "README.txt",
        "contentType": "text/plain",
        "filesize": 2072,
        "rootDataFileId": -1,
        "md5": "0c4cd4ed411caf3f127131c4dadda0ef",
        "checksum": {"type": "MD5", "value": "0c4cd4ed411caf3f127131c4dadda0ef"},
        "tabularData": False,
    },
}
_EMBARGOED = {
    "label": "Study2_populism.tab",
    "restricted": False,
    "version": 3,
    "datasetVersionId": 747429,
    "dataFile": {
        "id": 14242078,
        "persistentId": "",
        "filename": "Study2_populism.tab",
        "contentType": "text/tab-separated-values",
        "filesize": 4548030,
        "embargo": {
            "dateAvailable": "2028-01-31",
            "reason": "The data come from the PLEDGE project (Horizon Europe), and the "
            "consortium's data-sharing agreement requires this embargo.",
        },
        "originalFileFormat": "application/x-spss-sav",
        "originalFileSize": 3854043,
        "originalFileName": "Study2_populism.sav",
        "rootDataFileId": -1,
        "md5": "9e6a0bbf9bfb83c6ae62f708fcd76953",
        "checksum": {"type": "MD5", "value": "9e6a0bbf9bfb83c6ae62f708fcd76953"},
        "tabularData": True,
    },
}
# Verbatim from 10.7910/DVN/TJCLKP: an ingested file in a folder.
_IN_FOLDER = {
    "label": "2026-06-24.tab",
    "restricted": False,
    "directoryLabel": "data",
    "version": 3,
    "datasetVersionId": 736884,
    "dataFile": {
        "id": 14027661,
        "persistentId": "",
        "filename": "2026-06-24.tab",
        "contentType": "text/tab-separated-values",
        "filesize": 26344,
        "originalFileFormat": "text/tsv",
        "originalFileSize": 25314,
        "originalFileName": "2026-06-24.tsv",
        "rootDataFileId": 3035125,
        "previousDataFileId": 6867331,
        "md5": "0165275ec1437dd2bcb1c6b4b56ef233",
        "checksum": {"type": "MD5", "value": "0165275ec1437dd2bcb1c6b4b56ef233"},
        "tabularData": True,
        "directoryLabel": "data",
    },
}
# Verbatim from DataverseNL 10.34894/GRLULI: SHA-1, no md5.
_SHA1 = {
    "description": "",
    "label": "2029-07-30-ScholarsArchive@OSU-CoreTrustSealRequirements2023-2025.pdf",
    "restricted": False,
    "version": 1,
    "datasetVersionId": 35152,
    "dataFile": {
        "id": 657659,
        "persistentId": "",
        "filename": "2029-07-30-ScholarsArchive@OSU-CoreTrustSealRequirements2023-2025.pdf",
        "contentType": "application/pdf",
        "filesize": 255655,
        "rootDataFileId": -1,
        "checksum": {"type": "SHA-1", "value": "15aa639facdf9ee54dea2ffbd6cf9d8ea38d735e"},
        "tabularData": False,
    },
}
# Verbatim from the file endpoint for the file DOI 10.7910/DVN/BQFSOS/LVIXTT.
_FILE_ANSWER = {
    "status": "OK",
    "data": {
        "description": "http://pi.lib.uchicago.edu/1001/org/ochre/317d6aa4-51da-4a6b-b517-a29ee37ddfe2",
        "label": "A16_32702.jpg",
        "restricted": False,
        "version": 1,
        "datasetVersionId": 272238,
        "categories": ["Photo"],
        "dataFile": {
            "id": 5116805,
            "persistentId": "doi:10.7910/DVN/BQFSOS/LVIXTT",
            "pidURL": "https://doi.org/10.7910/DVN/BQFSOS/LVIXTT",
            "filename": "A16_32702.jpg",
            "contentType": "image/jpeg",
            "filesize": 5528970,
            "rootDataFileId": -1,
            "md5": "691b099e64c9ea5061ef92d140422854",
            "checksum": {"type": "MD5", "value": "691b099e64c9ea5061ef92d140422854"},
            "tabularData": False,
        },
    },
}
# Verbatim: the deaccessioned 10.7910/DVN/P7UKWR has no latestVersion.
_DEACCESSIONED = {
    "status": "OK",
    "data": {
        "id": 3005799,
        "identifier": "DVN/P7UKWR",
        "persistentUrl": "https://doi.org/10.7910/DVN/P7UKWR",
        "protocol": "doi",
        "authority": "10.7910",
        "separator": "/",
        "publisher": "Harvard Dataverse",
        "publicationDate": "2017-03-09",
        "storageIdentifier": "s3://10.7910/DVN/P7UKWR",
        "guestbookId": 110,
        "datasetType": "dataset",
        "locks": [],
    },
}
# Harvard's verbatim 404 for a DOI it holds no dataset under.
_NO_DATASET = (
    '{"status":"ERROR","message":"When accessing a dataset based on Persistent ID, '
    'a persistentId query parameter must be present."}'
)
_NO_FILE = '{"status":"ERROR","message":"Datafile with Persistent ID doi:10.7910/DVN/SJNBKL/MY5LEP not found."}'


def _dataset(*files: dict, dataset_id: int = 14209084) -> dict:
    return {
        "status": "OK",
        "data": {
            "id": dataset_id,
            "latestVersion": {"versionState": "RELEASED", "files": list(files)},
        },
    }


def _serving(routes: dict[str, tuple[int, object]], sent: list[httpx.Request]):
    """A client answering by URL path; anything else is a test failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        status, body = routes[request.url.path]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_DATASETS = "/api/datasets/:persistentId/"
_FILES = "/api/files/:persistentId/"


async def test_a_file_doi_lists_its_one_file():
    sent: list[httpx.Request] = []
    routes = {_DATASETS: (404, _NO_DATASET), _FILES: (200, _FILE_ANSWER)}
    async with _serving(routes, sent) as c:
        got = await dataverse.files(c, "10.7910/DVN/BQFSOS/LVIXTT")
    assert got == [
        FileEntry(
            name="A16_32702.jpg",
            size=5528970,
            url=f"{_DL}/5116805",
            checksum="md5:691b099e64c9ea5061ef92d140422854",
        )
    ]
    assert [(r.method, str(r.url)) for r in sent] == [
        ("GET", f"{_H}{_DATASETS}?persistentId=doi%3A10.7910%2FDVN%2FBQFSOS%2FLVIXTT"),
        ("GET", f"{_H}{_FILES}?persistentId=doi%3A10.7910%2FDVN%2FBQFSOS%2FLVIXTT"),
    ]
    # Positive control: a dataset DOI is listed from the dataset, and no file is asked.
    sent.clear()
    async with _serving({_DATASETS: (200, _dataset(_README))}, sent) as c:
        assert [f.name for f in await dataverse.files(c, "10.7910/DVN/18ERYS")] == ["README.txt"]
    assert [r.url.path for r in sent] == [_DATASETS]


async def test_a_doi_neither_endpoint_holds_lists_no_files(caplog):
    """A dataset the installation no longer holds is no file listing, as a deaccessioned
    one is, not "DataCite has no such DOI"."""
    sent: list[httpx.Request] = []
    routes = {_DATASETS: (404, _NO_DATASET), _FILES: (404, _NO_FILE)}
    with caplog.at_level(logging.WARNING, logger="data_aggregator_mcp.dataverse"):
        async with _serving(routes, sent) as c:
            assert await dataverse.files(c, "10.7910/DVN/SJNBKL/MY5LEP") == []
    assert [r.getMessage() for r in caplog.records] == [
        f"Dataverse DOI 10.7910/DVN/SJNBKL/MY5LEP: {_H} has no dataset or file with it; "
        "no file listing"
    ]
    assert [r.url.path for r in sent] == [_DATASETS, _FILES]


async def test_a_deaccessioned_dataset_lists_no_files():
    sent: list[httpx.Request] = []
    async with _serving({_DATASETS: (200, _DEACCESSIONED)}, sent) as c:
        assert await dataverse.files(c, "10.7910/DVN/P7UKWR") == []
    assert [r.url.path for r in sent] == [_DATASETS]  # not mistaken for a file DOI


def _on(day: str, **dates: str) -> dict:
    f = copy.deepcopy(_README)
    for key, value in dates.items():
        block, field = {
            "embargo": ("embargo", "dateAvailable"),
            "retention": ("retention", "dateUnavailable"),
        }[key]
        f["dataFile"][block] = {field: value, "reason": "probe"}
    f["label"] = day
    return f


async def test_an_embargoed_or_expired_file_is_not_listed(monkeypatch):
    """Dataverse refuses the download (403) while ``dateAvailable`` is after today, and
    once ``dateUnavailable`` is before today; on the day itself the file is served."""
    today = date(2026, 10, 2)
    monkeypatch.setattr(dataverse, "_today", lambda: today, raising=False)
    day = timedelta(days=1)
    files = [
        _on("embargo-ends-tomorrow", embargo=str(today + day)),
        _on("embargo-ends-today", embargo=str(today)),
        _on("embargo-ended", embargo=str(today - day)),
        _on("retention-ended-yesterday", retention=str(today - day)),
        _on("retention-ends-today", retention=str(today)),
        _on("retention-ends-tomorrow", retention=str(today + day)),
        _EMBARGOED,
        _README,
    ]
    async with _serving({_DATASETS: (200, _dataset(*files))}, []) as c:
        got = await dataverse.files(c, "10.7910/DVN/18ERYS")
    assert [f.name for f in got] == [
        "embargo-ends-today",
        "embargo-ended",
        "retention-ends-today",
        "retention-ends-tomorrow",
        "README.txt",
    ]


def test_today_is_the_local_date():
    assert dataverse._today() == date.today()


async def test_a_file_in_a_folder_keeps_its_folder():
    """``fetch`` keeps a name's folders so two files of one name in two folders stay
    two files (KU Leuven RDR 10.48804/OJUPQB has ``Arithmetic.m`` in two)."""
    twin = copy.deepcopy(_IN_FOLDER)
    twin["directoryLabel"] = "archive/2025"
    twin["dataFile"]["id"] = 7
    async with _serving({_DATASETS: (200, _dataset(_IN_FOLDER, twin, _README))}, []) as c:
        got = await dataverse.files(c, "10.7910/DVN/TJCLKP")
    assert [(f.name, f.url) for f in got] == [
        ("data/2026-06-24.tsv", f"{_DL}/14027661?format=original"),
        ("archive/2025/2026-06-24.tsv", f"{_DL}/7?format=original"),
        ("README.txt", f"{_DL}/14250294"),  # positive control: no folder, bare name
    ]


async def test_a_sha1_checksum_is_listed_as_sha1():
    no_algo = copy.deepcopy(_README)
    no_algo["dataFile"]["checksum"] = {"value": "0c4cd4ed411caf3f127131c4dadda0ef"}
    no_value = copy.deepcopy(_README)
    no_value["dataFile"]["checksum"] = {"type": "MD5"}
    body = _dataset(_SHA1, _README, no_algo, no_value, dataset_id=657658)
    async with _serving({_DATASETS: (200, body)}, []) as c:
        got = await dataverse.files(c, "10.34894/GRLULI", landing_url="https://dataverse.nl/x")
    assert [f.checksum for f in got] == [
        "sha1:15aa639facdf9ee54dea2ffbd6cf9d8ea38d735e",
        "md5:0c4cd4ed411caf3f127131c4dadda0ef",  # positive control
        None,
        None,
    ]


_DATASET_BROKEN = (
    r"^\[UpstreamUnavailableError\] Dataverse dataset returned an unparseable 200 body "
    r"after 3 tries: UpstreamEnvelopeError\([\"']no Dataverse dataset in "
)


@pytest.mark.parametrize(
    "body",
    [
        {},  # was read as zero files
        {"status": "OK"},
        {"status": "ERROR", "message": "boom"},
        {"status": "OK", "data": None},
        {"status": "OK", "data": {"latestVersion": {"files": []}}},  # no dataset id
        {"status": "OK", "data": {"id": "3005799"}},
        {"status": "OK", "data": {"id": True}},
        {"status": "OK", "data": {"id": 1, "latestVersion": "RELEASED"}},
        {"status": "OK", "data": {"id": 1, "latestVersion": {"files": "README.txt"}}},
        {"status": "OK", "data": {"id": 1, "latestVersion": {"files": [None]}}},
        _dataset({**_README, "label": None}),
        _dataset({**_README, "restricted": "false"}),
        _dataset({**_README, "dataFile": None}),
        _dataset({**_README, "dataFile": {**_README["dataFile"], "id": None}}),
        _dataset({**_README, "dataFile": {**_README["dataFile"], "embargo": {}}}),
        _dataset({**_README, "dataFile": {**_README["dataFile"], "retention": {"x": 1}}}),
        _dataset(
            {**_README, "dataFile": {**_README["dataFile"], "embargo": {"dateAvailable": "soon"}}}
        ),
        _dataset({**_README, "dataFile": {**_README["dataFile"], "checksum": {"type": 5}}}),
    ],
)
async def test_a_broken_dataset_answer_is_an_error_not_no_files(body):
    async with _serving({_DATASETS: (200, body)}, []) as c:
        with pytest.raises(UpstreamUnavailableError, match=_DATASET_BROKEN):
            await dataverse.files(c, "10.7910/DVN/18ERYS")
    # Positive control: the real answer is read.
    async with _serving({_DATASETS: (200, _dataset(_README))}, []) as c:
        assert len(await dataverse.files(c, "10.7910/DVN/18ERYS")) == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"status": "OK", "data": None}, {"status": "OK", "data": {"label": "a.jpg"}}],
)
async def test_a_broken_file_answer_is_an_error_not_no_files(body):
    routes = {_DATASETS: (404, _NO_DATASET), _FILES: (200, body)}
    async with _serving(routes, []) as c:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] Dataverse file returned an unparseable 200 "
            r"body after 3 tries: UpstreamEnvelopeError\([\"']no Dataverse file in ",
        ):
            await dataverse.files(c, "10.7910/DVN/BQFSOS/LVIXTT")
    routes[_FILES] = (200, _FILE_ANSWER)  # positive control
    async with _serving(routes, []) as c:
        assert len(await dataverse.files(c, "10.7910/DVN/BQFSOS/LVIXTT")) == 1


def test_the_checks_refuse_with_a_message_naming_dataverse():
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no Dataverse dataset in \{\}$"):
        dataverse._check_dataset({})
    with pytest.raises(_http.UpstreamEnvelopeError, match=r"^no Dataverse file in \{'x': 1\}$"):
        dataverse._check_file({"x": 1})
    long = {"data": "x" * 300}
    with pytest.raises(_http.UpstreamEnvelopeError) as err:
        dataverse._check_dataset(long)
    assert str(err.value) == f"no Dataverse dataset in {long!r:.200}"
    # Positive controls: the real answers pass.
    dataverse._check_dataset(_dataset(_README, _EMBARGOED, _IN_FOLDER, _SHA1))
    dataverse._check_dataset(_DEACCESSIONED)
    dataverse._check_file(_FILE_ANSWER)


# Every type a JSON value can take, one of each.
_WRONG = ["x", 7, True, 1.5, [1], {"k": 1}, None]


def _paths(node, path=()):
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], (*path, 0))


def _with(doc: dict, path: tuple, value: object) -> dict:
    rec = copy.deepcopy(doc)
    *parents, last = path
    node = rec
    for p in parents:
        node = node[p]
    node[last] = value
    return rec


_FULL_FILE = copy.deepcopy(_IN_FOLDER)
_FULL_FILE["dataFile"]["embargo"] = {"dateAvailable": "2020-01-31", "reason": "r"}
_FULL_FILE["dataFile"]["retention"] = {"dateUnavailable": "2099-01-31", "reason": "r"}


@pytest.mark.parametrize(
    ("body", "routes"),
    [
        (_dataset(_FULL_FILE), lambda b: {_DATASETS: (200, b)}),
        (
            {"status": "OK", "data": _FULL_FILE},
            lambda b: {_DATASETS: (404, _NO_DATASET), _FILES: (200, b)},
        ),
    ],
    ids=["dataset", "file"],
)
async def test_no_wrong_typed_field_escapes_as_a_bare_error(body, routes):
    """Walks every field of a real answer with every JSON type: each must be refused as
    a malformed answer or read cleanly (the datacite lesson of 2026-10-01)."""

    serving: dict[str, tuple[int, object]] = {}
    async with _serving(serving, []) as c:

        async def listing(b: dict) -> list[FileEntry]:
            serving.clear()
            serving.update(routes(b))
            return await dataverse.files(c, "10.7910/DVN/TJCLKP")

        # Positive control: the real shape is read whole.
        assert [f.name for f in await listing(body)] == ["data/2026-06-24.tsv"]
        escaped = []
        for path in _paths(body):
            if not path:
                continue
            for value in _WRONG:
                try:
                    await listing(_with(body, path, value))
                except UpstreamUnavailableError:
                    continue
                except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
                    escaped.append((path, value, type(exc).__name__))
    assert escaped == []


@live_only
async def test_live_a_file_doi_lists_its_file():
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        got = await dataverse.files(
            c,
            "10.7910/DVN/BQFSOS/LVIXTT",
            landing_url=f"{_H}/file.xhtml?persistentId=doi:10.7910/DVN/BQFSOS/LVIXTT",
        )
    assert [(f.name, f.size, f.url, f.checksum) for f in got] == [
        ("A16_32702.jpg", 5528970, f"{_DL}/5116805", "md5:691b099e64c9ea5061ef92d140422854")
    ]


@live_only
async def test_live_gone_and_deaccessioned_datasets_list_no_files():
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        assert await dataverse.files(c, "10.7910/DVN/SJNBKL/MY5LEP") == []
        assert await dataverse.files(c, "10.7910/DVN/P7UKWR") == []
        assert await dataverse.files(c, "10.7910/DVN/18ERYS")  # positive control


@live_only
async def test_live_an_embargoed_file_is_not_listed_and_its_download_is_refused():
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        got = await dataverse.files(c, "10.7910/DVN/18ERYS")
        refused = await c.get(f"{_DL}/14242078?format=original")
    names = {f.name for f in got}
    assert "README.txt" in names  # positive control
    assert not names & {"Study2_populism.sav", "Study2_populism.tab"}
    assert refused.status_code == 403


@live_only
async def test_live_a_sha1_checksum_matches_the_bytes_at_its_url():
    import hashlib

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        got = await dataverse.files(
            c, "10.34894/GRLULI", landing_url="https://dataverse.nl/citation?persistentId=x"
        )
        body = (await c.get(got[0].url)).content
    assert got[0].checksum == f"sha1:{hashlib.sha1(body).hexdigest()}"  # nosec B324 - sha1 is the upstream's checksum algorithm, compared not trusted


@live_only
async def test_live_files_in_folders_keep_their_folders():
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        got = await dataverse.files(
            c, "10.48804/OJUPQB", landing_url="https://rdr.kuleuven.be/dataset.xhtml"
        )
    names = [f.name for f in got]
    assert len(names) == len(set(names))
    assert "Bootstrapping_BGV_BFV-main/Digit extraction/Arithmetic.m" in names
    assert "Bootstrapping_BGV_BFV-traces/Digit extraction/Arithmetic.m" in names
