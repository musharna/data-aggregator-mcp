# Fetch-Breadth Wave (DataONE + OmicsDI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DataONE (MD5/SHA256-verified eco/env fetch) and OmicsDI (proteomics/metabolomics discovery) as search sources, with direct PRIDE + MetaboLights unverified-fetch backends.

**Architecture:** Four new adapter modules (`dataone.py`, `omicsdi.py`, `pride.py`, `metabolights.py`) following the `huggingface.py` shape, registered in `router._ADAPTERS` for search fan-out + resolve routing, with `server.py` fetch-allowlist + guard edits and two new `list_sources` entries. `fetch.py`/`models.py` are unchanged — DataONE rides the existing `<algo>:<hex>` checksum path; the MS backends ride the no-checksum / size-check path.

**Tech Stack:** Python 3, httpx (async), pydantic models, pytest + `httpx.MockTransport`. Design + verified API shapes: `docs/superpowers/specs/2026-05-31-fetch-breadth-dataone-omicsdi-design.md`.

**Branch:** Execute on a new branch `fetch-breadth-wave` off `main`. Ships **v0.18.0**.

**Conventions (match the codebase):**

- All HTTP goes through `data_aggregator_mcp._http.request_json(client, "GET", url, *, service=..., params=..., headers={"Accept": "application/json"}, timeout=..., max_retries=...)`. It returns parsed JSON and maps failures into the `DataAggregatorError` taxonomy. Pass `not_found_returns=None` to get `None` instead of a raised `NotFoundError` on 404.
- Search adapters return `tuple[int, list[DataResource]]` = `(total_hits, [compact(r), ...])`. `compact` (from `models`) drops `files[]` and truncates the description for search payloads.
- Resolve adapters return a full `DataResource` with `files[]` populated.
- A per-source ruff PostToolUse hook strips imports that are unused **at save time** — only add an import in the same edit that adds its first use (or the test will lose the import). Verify imports survive after each write.

**A note on test fixtures:** the JSON/XML fixtures below are real shapes captured live on 2026-05-31. Keep them verbatim — they are the synthetic half of the real-execution doctrine; the `@_live_only` tests are the real half.

---

### Task 1: PRIDE file-manifest backend (`pride.py`)

**Files:**

- Create: `src/data_aggregator_mcp/pride.py`
- Test: `tests/test_pride.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pride.py
import os

import httpx
import pytest

from data_aggregator_mcp import pride

_FILES = [
    {
        "fileName": "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz",
        "fileSizeBytes": 497985,
        "checksum": "",
        "publicFileLocations": [
            {"name": "FTP Protocol",
             "value": "ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz"},
            {"name": "Aspera Protocol",
             "value": "prd_ascp@fasp.ebi.ac.uk:pride/data/archive/2012/03/PXD000001/..."},
        ],
    },
    {"fileName": "no_public_loc.raw", "fileSizeBytes": 10, "publicFileLocations": []},
]


@pytest.mark.asyncio
async def test_files_rewrites_ftp_to_https_and_keeps_size():
    async def handler(request):
        assert request.url.path.endswith("/projects/PXD000001/files")
        return httpx.Response(200, json=_FILES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await pride.files(c, "PXD000001")
    assert len(files) == 1  # the no-public-location entry is dropped
    f = files[0]
    assert f.url == ("https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/"
                     "PXD000001/generated/PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz")
    assert f.size == 497985
    assert f.checksum is None  # PRIDE exposes no usable checksum
    assert f.source == "pride"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_pride_files_https_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await pride.files(c, "PXD000001")
        assert files, "PXD000001 should list files"
        head = await c.head(files[0].url)
        assert head.status_code < 400  # rewritten HTTPS url serves
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pride.py::test_files_rewrites_ftp_to_https_and_keeps_size -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_aggregator_mcp.pride'`

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/pride.py
"""PRIDE Archive (proteomics) file-manifest backend for OmicsDI-routed fetch.

PRIDE exposes no usable checksum (the v3 ``checksum`` field is empty), so files
are returned unverified — size-checked only. The public file URLs are ``ftp://``;
the same host serves over HTTPS, so we rewrite the scheme for httpx streaming.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

V3_FILES = "https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{acc}/files"
_FTP_HOST = "ftp://ftp.pride.ebi.ac.uk/"
_HTTPS_HOST = "https://ftp.pride.ebi.ac.uk/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


def _https_url(locations: list[dict] | None) -> str | None:
    """Pick a public location and return an httpx-streamable HTTPS url, or None."""
    for loc in locations or []:
        val = loc.get("value") or ""
        if val.startswith(_FTP_HOST):
            return _HTTPS_HOST + val[len(_FTP_HOST):]
        if val.startswith("https://"):
            return val
    return None


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    body = await _http.request_json(
        client,
        "GET",
        V3_FILES.format(acc=accession),
        service="PRIDE files",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    entries = body if isinstance(body, list) else []
    out: list[FileEntry] = []
    for e in entries:
        url = _https_url(e.get("publicFileLocations"))
        if not url:
            continue
        out.append(
            FileEntry(
                name=e.get("fileName", ""),
                url=url,
                size=e.get("fileSizeBytes"),
                checksum=None,
                source="pride",
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pride.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/pride.py tests/test_pride.py
git commit -m "feat(pride): file-manifest backend (ftp→https rewrite, unverified)"
```

---

### Task 2: MetaboLights file-manifest backend (`metabolights.py`)

**Files:**

- Create: `src/data_aggregator_mcp/metabolights.py`
- Test: `tests/test_metabolights.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metabolights.py
import os

import httpx
import pytest

from data_aggregator_mcp import metabolights

_BODY = {
    "study": [
        {"file": "m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv",
         "type": "metadata_maf", "status": "active", "directory": False},
        {"file": "RAW", "type": "raw", "status": "active", "directory": True},
    ],
    "latest": [],
}


@pytest.mark.asyncio
async def test_files_builds_https_urls_no_checksum_no_size():
    async def handler(request):
        assert request.url.path.endswith("/studies/MTBLS1/files")
        assert request.url.params["include_raw_data"] == "false"
        return httpx.Response(200, json=_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        files = await metabolights.files(c, "MTBLS1")
    assert len(files) == 1  # the directory entry is dropped
    f = files[0]
    assert f.url == ("https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/"
                     "MTBLS1/m_MTBLS1_metabolite_profiling_NMR_spectroscopy_v2_maf.tsv")
    assert f.checksum is None and f.size is None
    assert f.source == "metabolights"


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_metabolights_files_serves():
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        files = await metabolights.files(c, "MTBLS1")
        assert files
        head = await c.head(files[0].url)
        assert head.status_code < 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metabolights.py::test_files_builds_https_urls_no_checksum_no_size -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_aggregator_mcp.metabolights'`

- [ ] **Step 3: Write the implementation**

```python
# src/data_aggregator_mcp/metabolights.py
"""MetaboLights (metabolomics) file-manifest backend for OmicsDI-routed fetch.

The MetaboLights files API exposes neither checksum nor size, so files are
returned fully unverified. Bytes are served from the EBI FTP HTTPS mirror.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import FileEntry

WS_FILES = "https://www.ebi.ac.uk/metabolights/ws/studies/{acc}/files"
FTP_BASE = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{acc}/{file}"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2


async def files(client: httpx.AsyncClient, accession: str) -> list[FileEntry]:
    body = await _http.request_json(
        client,
        "GET",
        WS_FILES.format(acc=accession),
        service="MetaboLights files",
        params={"include_raw_data": "false"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    study = (body or {}).get("study") or []
    out: list[FileEntry] = []
    for e in study:
        fname = e.get("file")
        if not fname or e.get("directory"):
            continue
        out.append(
            FileEntry(
                name=fname,
                url=FTP_BASE.format(acc=accession, file=fname),
                size=None,
                checksum=None,
                source="metabolights",
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_metabolights.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/metabolights.py tests/test_metabolights.py
git commit -m "feat(metabolights): file-manifest backend (https mirror, unverified)"
```

---

### Task 3: DataONE search (`dataone.py` — search half)

**Files:**

- Create: `src/data_aggregator_mcp/dataone.py`
- Test: `tests/test_dataone.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dataone.py
import os

import httpx
import pytest

from data_aggregator_mcp import dataone
from data_aggregator_mcp.models import Creator

_SEARCH = {
    "response": {
        "numFound": 17069,
        "docs": [
            {"identifier": "doi:10.18739/A26336", "title": "Soil Probes",
             "origin": ["Jane Doe", "John Roe"], "datePublished": "2015-06-01T00:00:00Z",
             "dateUploaded": "2015-05-01T00:00:00Z", "dateModified": "2016-01-01T00:00:00Z",
             "resourceMap": ["resource_map_doi:10.18739/A26336"]},
            {"identifier": "knb-lter-jrn.20050360.9823", "title": "Soil Nematodes",
             "author": "John Anderson", "dateUploaded": "2011-12-03T00:00:00Z"},
        ],
    }
}


@pytest.mark.asyncio
async def test_search_normalizes_and_prefixes_id():
    async def handler(request):
        assert request.url.path.endswith("/query/solr/")
        assert "formatType:METADATA" in request.url.params["q"]
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await dataone.search(c, "soil", size=10)
    assert total == 17069
    r0 = recs[0]
    assert r0.id == "dataone:doi:10.18739/A26336" and r0.source == "dataone"
    assert r0.kind == "dataset" and r0.year == 2015
    assert r0.creators == [Creator(name="Jane Doe"), Creator(name="John Roe")]
    assert r0.last_updated == "2016-01-01T00:00:00Z"
    assert r0.files == []  # compact() drops files in search payloads
    # single-author fallback when origin absent
    assert recs[1].creators == [Creator(name="John Anderson")] and recs[1].year == 2011


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search():
    async with httpx.AsyncClient(timeout=60) as c:
        total, recs = await dataone.search(c, "soil", size=5)
        assert total > 0 and recs
        assert recs[0].id.startswith("dataone:") and recs[0].source == "dataone"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dataone.py::test_search_normalizes_and_prefixes_id -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_aggregator_mcp.dataone'`

- [ ] **Step 3: Write the implementation (search half only)**

```python
# src/data_aggregator_mcp/dataone.py
"""DataONE federation (eco/environmental) — search + resolve + verified fetch.

Discovery hits the Coordinating Node Solr index. Data bytes live on Member
Nodes, so ``resolve`` does a per-object ``/resolve/`` hop (Task 4) to get the
streamable MN url. Checksums vary per object (MD5 or SHA256); the prefix is
built from ``checksumAlgorithm`` so ``fetch.py``'s ``_hasher`` verifies either.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import Creator, DataResource, compact

SOLR = "https://cn.dataone.org/cn/v2/query/solr/"
PREFIXES = {"dataone"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

_SEARCH_FL = (
    "identifier,title,author,origin,formatId,dateUploaded,datePublished,dateModified,resourceMap"
)


def _year(*vals: str | None) -> int | None:
    for v in vals:
        if v and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    return None


def _creators(doc: dict) -> list[Creator]:
    origin = doc.get("origin")
    if isinstance(origin, list) and origin:
        return [Creator(name=str(o)) for o in origin if o]
    author = doc.get("author")
    return [Creator(name=str(author))] if author else []


def _normalize(doc: dict) -> DataResource:
    pid = doc.get("identifier", "")
    return DataResource(
        id=f"dataone:{pid}",
        source="dataone",
        kind="dataset",
        title=doc.get("title") or "",
        creators=_creators(doc),
        year=_year(doc.get("datePublished"), doc.get("dateUploaded")),
        last_updated=doc.get("dateModified"),
        files=[],
    )


async def _solr(
    client: httpx.AsyncClient, query: str, *, rows: int, fl: str, start: int = 0
) -> tuple[int, list[dict]]:
    body = await _http.request_json(
        client,
        "GET",
        SOLR,
        service="DataONE search",
        params={"q": query, "fl": fl, "rows": str(rows), "start": str(start), "wt": "json"},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    resp = body.get("response", {}) or {}
    return int(resp.get("numFound", 0)), (resp.get("docs") or [])


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    capped = min(size, MAX_SIZE)
    q = f"({query}) AND formatType:METADATA"
    total, docs = await _solr(client, q, rows=capped, start=offset, fl=_SEARCH_FL)
    return total, [compact(_normalize(d)) for d in docs]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dataone.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/dataone.py tests/test_dataone.py
git commit -m "feat(dataone): Solr search adapter"
```

---

### Task 4: DataONE resolve + verified files (`dataone.py` — resolve half)

**Files:**

- Modify: `src/data_aggregator_mcp/dataone.py` (add resolve + helpers)
- Test: `tests/test_dataone.py` (add resolve tests)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_dataone.py
import hashlib

from data_aggregator_mcp.errors import NotFoundError

_META_DOC = {"response": {"numFound": 1, "docs": [{
    "identifier": "doi:10.18739/A26336", "title": "Soil Probes",
    "origin": ["Jane Doe"], "datePublished": "2015-06-01T00:00:00Z",
    "resourceMap": ["resource_map_doi:10.18739/A26336"]}]}}

_DATA_DOCS = {"response": {"numFound": 1, "docs": [{
    "identifier": "urn:uuid:e2919f95-81e2-4aec-a6f0-b46861c1822b",
    "fileName": "Probes2011.xlsx", "size": 28270,
    "checksum": "3640858c8d60658422b619ea34f5b1afc1be4903ea3948ed68261cbea76e11d0",
    "checksumAlgorithm": "SHA256"}]}}

_OBJLOC = (
    '<?xml version="1.0"?><ns2:objectLocationList '
    'xmlns:ns2="http://ns.dataone.org/service/types/v1">'
    '<objectLocation><url>https://arcticdata.io/metacat/d1/mn/v2/object/'
    'urn:uuid:e2919f95-81e2-4aec-a6f0-b46861c1822b</url></objectLocation>'
    '</ns2:objectLocationList>'
)


@pytest.mark.asyncio
async def test_resolve_attaches_data_files_with_checksum():
    def handler(request):
        p, q = request.url.path, request.url.params.get("q", "")
        if p.endswith("/query/solr/") and "identifier:" in q:
            return httpx.Response(200, json=_META_DOC)
        if p.endswith("/query/solr/") and "formatType:DATA" in q:
            return httpx.Response(200, json=_DATA_DOCS)
        if "/resolve/" in p:
            return httpx.Response(200, text=_OBJLOC)
        raise AssertionError(f"unexpected request {p}?{q}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await dataone.resolve(c, "dataone:doi:10.18739/A26336")
    assert len(r.files) == 1
    f = r.files[0]
    assert f.name == "Probes2011.xlsx" and f.size == 28270
    assert f.checksum == ("sha256:3640858c8d60658422b619ea34f5b1afc1be4903ea3948ed68261cbea76e11d0")
    assert f.url.startswith("https://arcticdata.io/")


@pytest.mark.asyncio
async def test_resolve_no_resource_map_returns_empty_files():
    doc = {"response": {"numFound": 1, "docs": [{"identifier": "x", "title": "t"}]}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=doc))
    ) as c:
        r = await dataone.resolve(c, "dataone:x")
    assert r.files == []


@pytest.mark.asyncio
async def test_resolve_404_when_no_doc():
    empty = {"response": {"numFound": 0, "docs": []}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=empty))
    ) as c:
        with pytest.raises(NotFoundError):
            await dataone.resolve(c, "dataone:missing")


@_live_only
@pytest.mark.asyncio
async def test_live_resolve_and_fetch_verifies_md5_or_sha():
    from data_aggregator_mcp import fetch
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        r = await dataone.resolve(c, "dataone:doi:10.18739/A26336")
        assert r.files and r.files[0].checksum  # a verified file is present
        # fetch one small file end-to-end; fetch.py raises on checksum mismatch
        small = min(r.files, key=lambda f: f.size or 1 << 62)
        res = await fetch.fetch_files(c, r.model_copy(update={"files": [small]}),
                                      dest=os.environ.get("CLAUDE_JOB_DIR", "/tmp") + "/d1test")
        assert res.paths
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dataone.py::test_resolve_attaches_data_files_with_checksum -v`
Expected: FAIL — `AttributeError: module 'data_aggregator_mcp.dataone' has no attribute 'resolve'`

- [ ] **Step 3: Write the implementation (add to `dataone.py`)**

Add these imports at the top of `dataone.py` (in the same edit as the code that uses them, so ruff keeps them):

```python
import asyncio
from urllib.parse import quote
from xml.etree import ElementTree as ET

from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry
```

Add this module constant near `SOLR`:

```python
RESOLVE = "https://cn.dataone.org/cn/v2/resolve/{pid}"
_RESOLVE_FL = "identifier,title,author,origin,dateUploaded,datePublished,dateModified,resourceMap"
_DATA_FL = "identifier,fileName,size,checksum,checksumAlgorithm"
```

Add these functions:

```python
def _first_url(xml_text: str) -> str | None:
    """First <url> in a DataONE ObjectLocationList (namespace-agnostic), or None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "url" and el.text:
            return el.text.strip()
    return None


async def _object_url(client: httpx.AsyncClient, pid: str) -> str | None:
    """Resolve a data PID to a Member-Node byte url (CN /object 404s for MN-only
    objects, so we must read the ObjectLocationList)."""
    resp = await client.get(RESOLVE.format(pid=quote(pid, safe="")), timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        return None
    return _first_url(resp.text)


async def _file_entry(client: httpx.AsyncClient, doc: dict) -> FileEntry | None:
    pid = doc.get("identifier")
    if not pid:
        return None
    url = await _object_url(client, pid)
    if not url:
        return None
    algo, cs = doc.get("checksumAlgorithm"), doc.get("checksum")
    checksum = f"{algo.lower()}:{cs}" if algo and cs else None
    return FileEntry(
        name=doc.get("fileName") or pid, url=url, size=doc.get("size"),
        checksum=checksum, source="dataone",
    )


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    pid = resource_id.split(":", 1)[1] if resource_id.startswith("dataone:") else resource_id
    _total, docs = await _solr(client, f'identifier:"{pid}"', rows=1, fl=_RESOLVE_FL)
    if not docs:
        raise NotFoundError(f"DataONE has no object {pid!r}")
    resource = _normalize(docs[0])
    rmaps = docs[0].get("resourceMap")
    rmap = rmaps[0] if isinstance(rmaps, list) and rmaps else None
    if not rmap:
        return resource  # metadata-only package
    _t, data_docs = await _solr(
        client, f'resourceMap:"{rmap}" AND formatType:DATA', rows=MAX_SIZE, fl=_DATA_FL
    )
    entries = await asyncio.gather(*[_file_entry(client, d) for d in data_docs])
    return resource.model_copy(update={"files": [e for e in entries if e is not None]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dataone.py -v -k "not live"`
Expected: PASS (4 unit tests)

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/dataone.py tests/test_dataone.py
git commit -m "feat(dataone): resolve with MN-url hop + verified checksums"
```

---

### Task 5: OmicsDI search (`omicsdi.py` — search half)

**Files:**

- Create: `src/data_aggregator_mcp/omicsdi.py`
- Test: `tests/test_omicsdi.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_omicsdi.py
import os

import httpx
import pytest

from data_aggregator_mcp import omicsdi

_SEARCH = {
    "count": 740517,
    "datasets": [
        {"id": "MTBLS1355", "source": "metabolights_dataset", "title": "Breast Cancer Metabolomics",
         "description": "A metabolomics study."},
        {"id": "PXD000001", "source": "pride", "title": "TMT spike-in", "description": "Proteomics."},
        {"id": "GSE12345", "source": "omics_geo", "title": "Some transcriptomics", "description": "RNA."},
    ],
}


@pytest.mark.asyncio
async def test_search_keeps_only_modality_repos():
    async def handler(request):
        assert request.url.path.endswith("/dataset/search")
        return httpx.Response(200, json=_SEARCH)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await omicsdi.search(c, "cancer", size=10)
    ids = [r.id for r in recs]
    assert ids == ["omicsdi:metabolights_dataset:MTBLS1355", "omicsdi:pride:PXD000001"]
    assert total == 2  # GEO hit dropped; total is the kept count
    assert recs[0].source == "omicsdi" and recs[0].kind == "study"
    assert recs[0].files == []


@pytest.mark.asyncio
async def test_search_offset_returns_empty():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_SEARCH))
    ) as c:
        assert await omicsdi.search(c, "x", size=10, offset=10) == (0, [])


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_modality_only():
    async with httpx.AsyncClient(timeout=60) as c:
        _total, recs = await omicsdi.search(c, "cancer", size=20)
        assert all(r.id.split(":")[1] in omicsdi._MODALITY_REPOS for r in recs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_omicsdi.py::test_search_keeps_only_modality_repos -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data_aggregator_mcp.omicsdi'`

- [ ] **Step 3: Write the implementation (search half)**

```python
# src/data_aggregator_mcp/omicsdi.py
"""OmicsDI (Omics Discovery Index) — proteomics/metabolomics discovery.

Restricted to the mass-spec modality repos OmicsDI uniquely adds; GEO /
ArrayExpress / ENA hits are dropped (already covered by the omics leg, and
accession-keyed so the DOI dedup would miss the duplicates). Resolve (Task 6)
routes fetchable files to PRIDE / MetaboLights; other repos are discovery-only.

Page-1-only: we post-filter each page to the modality repos, so the router's
offset accounting (which counts records consumed from the merged stream) cannot
be reconciled with the upstream all-rows offset — mirror huggingface.py and
contribute first-page results only.
"""

from __future__ import annotations

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.models import DataResource, Link, compact

SEARCH = "https://www.omicsdi.org/ws/dataset/search"
RECORD = "https://www.omicsdi.org/ws/dataset/{source}/{acc}"
_LANDING = "https://www.omicsdi.org/dataset/{source}/{acc}"
PREFIXES = {"omicsdi"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 2

# OmicsDI `source` codes for the mass-spec modality we uniquely add.
_MODALITY_REPOS = {
    "pride",
    "massive",
    "metabolights_dataset",
    "metabolomics_workbench",
    "gnps",
    "peptide_atlas",
    "ega",
}


def _normalize(d: dict) -> DataResource:
    source, acc = d.get("source", ""), d.get("id", "")
    return DataResource(
        id=f"omicsdi:{source}:{acc}",
        source="omicsdi",
        kind="study",
        title=d.get("title") or "",
        description=d.get("description"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))],
        files=[],
    )


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    if offset:  # page-1-only (see module docstring)
        return 0, []
    body = await _http.request_json(
        client,
        "GET",
        SEARCH,
        service="OmicsDI search",
        params={"query": query, "size": min(size, MAX_SIZE)},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    datasets = (body or {}).get("datasets") or []
    kept = [d for d in datasets if d.get("source") in _MODALITY_REPOS]
    return len(kept), [compact(_normalize(d)) for d in kept]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_omicsdi.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/omicsdi.py tests/test_omicsdi.py
git commit -m "feat(omicsdi): modality-restricted search (page-1-only)"
```

---

### Task 6: OmicsDI resolve with fetch routing (`omicsdi.py` — resolve half)

**Files:**

- Modify: `src/data_aggregator_mcp/omicsdi.py` (add resolve)
- Test: `tests/test_omicsdi.py` (add resolve tests)

**Note on the GET record shape (verified live):** `GET /ws/dataset/{source}/{acc}` returns `{accession, name, description, database, dates, cross_references, ...}` — it uses `name` (not `title`) and has no `id`/`source`/`title` keys. So resolve builds the resource from the parsed id parts + the GET body's `name`/`description`; it does NOT reuse `_normalize` on the GET body.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_omicsdi.py
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import FileEntry

_RECORD = {"accession": "PXD000001", "name": "TMT spike-in", "description": "Proteomics study."}


@pytest.mark.asyncio
async def test_resolve_pride_routes_to_pride_files(monkeypatch):
    async def fake_pride_files(client, acc):
        assert acc == "PXD000001"
        return [FileEntry(name="a.raw", url="https://ftp.pride.ebi.ac.uk/a.raw", source="pride")]

    monkeypatch.setattr("data_aggregator_mcp.pride.files", fake_pride_files)

    async def handler(request):
        assert request.url.path.endswith("/dataset/pride/PXD000001")
        return httpx.Response(200, json=_RECORD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await omicsdi.resolve(c, "omicsdi:pride:PXD000001")
    assert r.id == "omicsdi:pride:PXD000001" and r.title == "TMT spike-in"
    assert [f.name for f in r.files] == ["a.raw"]
    assert any(lnk.rel == "landing_page" for lnk in r.links)


@pytest.mark.asyncio
async def test_resolve_non_fetchable_repo_has_empty_files():
    rec = {"accession": "PXD9", "name": "x", "description": "y"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rec))
    ) as c:
        r = await omicsdi.resolve(c, "omicsdi:massive:MSV000001")
    assert r.files == []  # MassIVE is discovery-only this wave


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await omicsdi.resolve(c, "omicsdi:onlytwo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_omicsdi.py::test_resolve_pride_routes_to_pride_files -v`
Expected: FAIL — `AttributeError: module 'data_aggregator_mcp.omicsdi' has no attribute 'resolve'`

- [ ] **Step 3: Write the implementation (add to `omicsdi.py`)**

Add to the imports (same edit as the code using them):

```python
from data_aggregator_mcp import metabolights, pride
from data_aggregator_mcp.errors import NotFoundError
```

Add the function:

```python
async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    parts = resource_id.split(":", 2)  # omicsdi:<source>:<acc>
    if len(parts) != 3:
        raise NotFoundError(f"malformed OmicsDI id {resource_id!r}")
    _prefix, source, acc = parts
    body = await _http.request_json(
        client,
        "GET",
        RECORD.format(source=source, acc=acc),
        service="OmicsDI resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if body is None:
        raise NotFoundError(f"OmicsDI has no {source}/{acc}")
    resource = DataResource(
        id=resource_id,
        source="omicsdi",
        kind="study",
        title=body.get("name") or "",
        description=body.get("description"),
        links=[Link(rel="landing_page", target_id=_LANDING.format(source=source, acc=acc))],
        files=[],
    )
    if source == "pride":
        file_list = await pride.files(client, acc)
    elif source == "metabolights_dataset":
        file_list = await metabolights.files(client, acc)
    else:
        file_list = []
    return resource.model_copy(update={"files": file_list})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_omicsdi.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/omicsdi.py tests/test_omicsdi.py
git commit -m "feat(omicsdi): resolve routing to PRIDE/MetaboLights fetch"
```

---

### Task 7: Register DataONE + OmicsDI in the router

**Files:**

- Modify: `src/data_aggregator_mcp/router.py` (`_ADAPTERS` dict ~48-54; `resolve` routing ~396-413)
- Test: `tests/test_router.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_router.py
import httpx
import pytest

from data_aggregator_mcp import router


def test_dataone_and_omicsdi_registered_in_precedence_order():
    names = list(router._ADAPTERS)
    assert "dataone" in names and "omicsdi" in names
    # dataone before datacite (keep the verified copy on a DOI tie); omicsdi last
    assert names.index("dataone") < names.index("datacite")
    assert names[-1] == "omicsdi"


@pytest.mark.asyncio
async def test_resolve_routes_dataone_prefix(monkeypatch):
    called = {}

    async def fake(client, rid):
        called["rid"] = rid
        from data_aggregator_mcp.models import DataResource
        return DataResource(id=rid, source="dataone", kind="dataset", title="t")

    monkeypatch.setattr("data_aggregator_mcp.dataone.resolve", fake)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "dataone:doi:10.5/x")
    assert called["rid"] == "dataone:doi:10.5/x" and r.source == "dataone"


@pytest.mark.asyncio
async def test_resolve_routes_omicsdi_prefix(monkeypatch):
    async def fake(client, rid):
        from data_aggregator_mcp.models import DataResource
        return DataResource(id=rid, source="omicsdi", kind="study", title="t")

    monkeypatch.setattr("data_aggregator_mcp.omicsdi.resolve", fake)
    async with httpx.AsyncClient() as c:
        r = await router.resolve(c, "omicsdi:pride:PXD000001")
    assert r.source == "omicsdi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router.py::test_dataone_and_omicsdi_registered_in_precedence_order -v`
Expected: FAIL — `KeyError`/`assert` (dataone not in `_ADAPTERS`)

- [ ] **Step 3: Write the implementation**

In `router.py`, extend the import block (the `from data_aggregator_mcp import (...)` around lines 20-29) to include `dataone` and `omicsdi`:

```python
from data_aggregator_mcp import (
    _cursor,
    datacite,
    dataone,
    embeddings,
    huggingface,
    literature,
    omics,
    omicsdi,
    taxonomy,
    zenodo,
)
```

Replace the `_ADAPTERS` dict (currently lines ~48-54) with:

```python
_ADAPTERS: dict[str, Any] = {
    "zenodo": zenodo,
    "dataone": dataone,
    "datacite": datacite,
    "omics": omics,
    "literature": literature,
    "huggingface": huggingface,
    "omicsdi": omicsdi,
}
```

In `resolve` (the prefix routing, currently ~396-413), add the two new branches. Put the `dataone`/`omicsdi` checks before the bare-DOI fallback:

```python
    prefix = rid.split(":", 1)[0]
    if prefix in omics.PREFIXES:
        resource = await omics.resolve(client, rid)
    elif prefix in literature.PREFIXES:
        resource = await literature.resolve(client, rid)
    elif prefix in dataone.PREFIXES:
        resource = await dataone.resolve(client, rid)
    elif prefix in omicsdi.PREFIXES:
        resource = await omicsdi.resolve(client, rid)
    elif rid.startswith("datacite:"):
        resource = await datacite.resolve(client, rid)
    elif rid.startswith("zenodo:") or rid.isdigit():
        resource = await zenodo.resolve(client, rid)
    elif prefix in huggingface.PREFIXES:
        resource = await huggingface.resolve(client, rid)
    elif "/" in rid:
        resource = await datacite.resolve(client, rid)
    else:
        raise ValueError(
            f"cannot route id {resource_id!r}: expected 'zenodo:<id>', 'datacite:<doi>', "
            "'dataone:<pid>', 'omicsdi:<source>:<acc>', 'geo:/sra:/bioproject:<acc>', "
            "'pubmed:/openaire:<id>', a bare Zenodo id, or a DOI"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_router.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/router.py tests/test_router.py
git commit -m "feat(router): register DataONE + OmicsDI (search fan-out + resolve)"
```

---

### Task 8: Server fetch allowlist + OmicsDI fetch guard

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (`_FETCHABLE_SOURCES` ~34-42; add `_ensure_omicsdi_fetchable`; call it in the fetch dispatch ~502-503)
- Test: `tests/test_server.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_server.py
import pytest

from data_aggregator_mcp import server
from data_aggregator_mcp.errors import FetchNotSupportedError
from data_aggregator_mcp.models import DataResource, FileEntry


def test_dataone_and_omicsdi_are_fetchable_prefixes():
    assert server._is_fetchable("dataone:doi:10.5/x")
    assert server._is_fetchable("omicsdi:pride:PXD000001")


def test_ensure_omicsdi_fetchable_raises_when_no_files():
    r = DataResource(id="omicsdi:massive:MSV1", source="omicsdi", kind="study",
                     title="x", files=[])
    with pytest.raises(FetchNotSupportedError):
        server._ensure_omicsdi_fetchable("omicsdi:massive:MSV1", r)


def test_ensure_omicsdi_fetchable_passes_when_files_present():
    r = DataResource(id="omicsdi:pride:PXD1", source="omicsdi", kind="study", title="x",
                     files=[FileEntry(name="a", url="https://x/a")])
    server._ensure_omicsdi_fetchable("omicsdi:pride:PXD1", r)  # no raise


def test_ensure_omicsdi_fetchable_ignores_non_omicsdi_ids():
    r = DataResource(id="zenodo:1", source="zenodo", kind="dataset", title="x", files=[])
    server._ensure_omicsdi_fetchable("zenodo:1", r)  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_dataone_and_omicsdi_are_fetchable_prefixes -v`
Expected: FAIL — `assert False` (`dataone:` not in `_FETCHABLE_SOURCES`)

- [ ] **Step 3: Write the implementation**

Extend `_FETCHABLE_SOURCES` (server.py ~34-42):

```python
_FETCHABLE_SOURCES = (
    "zenodo:",
    "sra:",
    "geo:",
    "datacite:",
    "pubmed:",
    "openaire:",
    "hf:",
    "dataone:",
    "omicsdi:",
)  # id prefixes with a working fetch backend
```

Add a guard next to `_ensure_repo_fetchable` (after it, ~line 66):

```python
def _ensure_omicsdi_fetchable(fid: str, resource: DataResource) -> None:
    """Fail loud when an omicsdi: id resolved to no files — its repo (MassIVE,
    Metabolomics Workbench, GNPS, PeptideAtlas, EGA) is discovery-only this wave.
    PRIDE/MetaboLights populate files[] at resolve and pass."""
    if fid.startswith("omicsdi:") and not resource.files:
        landing = next((lnk.target_id for lnk in resource.links if lnk.rel == "landing_page"), None)
        where = f" Fetch from the source repo directly: {landing}" if landing else ""
        raise FetchNotSupportedError(
            f"'{fid}' is discovery-only for fetch — only PRIDE and MetaboLights records "
            f"are streamable; this repo exposes no wired fetch backend.{where}"
        )
```

Call it in the fetch dispatch, right after `_ensure_fulltext_available(fid, resource)` (server.py ~503):

```python
                _ensure_repo_fetchable(fid, resource)
                _ensure_fulltext_available(fid, resource)
                _ensure_omicsdi_fetchable(fid, resource)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): fetch allowlist + OmicsDI discovery-only guard"
```

---

### Task 9: list_sources entries for DataONE + OmicsDI

**Files:**

- Modify: `src/data_aggregator_mcp/server.py` (`_SOURCES` list ~84+)
- Test: `tests/test_server.py` (add a case)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_server.py
def test_list_sources_includes_dataone_and_omicsdi():
    by_name = {s["name"]: s for s in server._SOURCES}
    assert "dataone" in by_name and "omicsdi" in by_name
    d1 = by_name["dataone"]
    assert d1["layer"] == "archives" and d1["fetchable"] is True
    assert "md5" in d1["fetchable_notes"].lower() or "sha" in d1["fetchable_notes"].lower()
    od = by_name["omicsdi"]
    assert od["layer"] == "omics" and od["fetchable"] == "per-repo"
    assert "id_example" in d1 and "id_example" in od
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_list_sources_includes_dataone_and_omicsdi -v`
Expected: FAIL — `assert "dataone" in by_name`

- [ ] **Step 3: Write the implementation**

Append two dicts to the `_SOURCES` list in `server.py` (after the existing entries, before the closing `]`). Match the shape of the existing entries (keys: `name`, `layer`, `kinds`, `filters_supported`, `auth_required`, `rate_limit`, `status`, `fetchable`, `id_example`, optional `fetchable_notes`, optional `description`):

```python
    {
        "name": "dataone",
        "layer": "archives",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size", "cursor"],
        "auth_required": False,
        "rate_limit": "public CN; courtesy only",
        "status": "live (eco/environmental federation; verified fetch via Member Nodes)",
        "fetchable": True,
        "fetchable_notes": "Data objects fetched from Member Nodes with per-object MD5/SHA-256 verification.",
        "id_example": "dataone:doi:10.18739/A26336",
        "description": "DataONE federation of environmental & earth-science repositories (KNB, Arctic Data Center, PANGAEA, TERN, …).",
    },
    {
        "name": "omicsdi",
        "layer": "omics",
        "kinds": ["study"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (proteomics/metabolomics discovery; first page only)",
        "fetchable": "per-repo",
        "fetchable_notes": "PRIDE + MetaboLights records are fetchable (unverified — no upstream checksum); MassIVE/Metabolomics Workbench/GNPS/PeptideAtlas/EGA are discovery-only.",
        "id_example": "omicsdi:pride:PXD000001",
        "description": "Omics Discovery Index — proteomics & metabolomics studies; restricted to the mass-spec modality repos not already covered by the omics leg.",
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py -v -k "not live"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data_aggregator_mcp/server.py tests/test_server.py
git commit -m "feat(server): list_sources entries for DataONE + OmicsDI"
```

---

### Task 10: README refresh

**Files:**

- Modify: `README.md` (Sources intro ~12-17; Sources table ~103-122; fetch list ~185-188)

- [ ] **Step 1: Update the intro sentence** (~lines 12-17) to name the new sources. Change the `search` one-liner to read:

```text
`search` one query across **Zenodo, DataCite** (Dryad / Figshare / Dataverse /
OSF / Mendeley), **NCBI omics** (GEO / SRA / BioProject), **DataONE** (eco /
environmental), **literature** (PubMed / OpenAIRE), **OmicsDI** (proteomics /
metabolomics), and **HuggingFace** datasets — deduplicated, normalized, and
cross-linked. `resolve` any hit to its file manifest, citation, trust signals,
and the data it points at. `fetch` it to disk with checksum verification.
```

- [ ] **Step 2: Add two rows to the Sources table** (after the HuggingFace row, ~line 117), keeping the column alignment:

```text
| DataONE (eco/env)            |    ✅    | ✅ (Member Node)  |   md5 / sha-256  |
| OmicsDI → PRIDE              |    ✅    |  ✅ (HTTPS FTP)   |     size only    |
| OmicsDI → MetaboLights       |    ✅    |  ✅ (HTTPS FTP)   |       none       |
| OmicsDI → other MS repos     |    ✅    |         —         |        —         |
```

- [ ] **Step 3: Update the fetchable list** in the `fetch` section (~lines 185-188) to add DataONE and the MS repos:

```text
- Fetchable: **Zenodo**, **SRA**, **GEO**, **DataONE** (Member-Node objects,
  md5/sha-256 verified), DataCite-hosted **Figshare** / **Dataverse** / **OSF**,
  **HuggingFace** datasets, **PRIDE** / **MetaboLights** (via OmicsDI, unverified),
  and **literature** open-access full text. **Dryad**, other DataCite repos, and
  other OmicsDI repos (MassIVE / GNPS / …) are discovery-only and raise
  `FetchNotSupportedError`.
```

- [ ] **Step 4: Verify the doc tests still pass** (the README is referenced by no test assertion, but run the suite to be safe):

Run: `uv run pytest tests/test_packaging.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(readme): add DataONE + OmicsDI/PRIDE/MetaboLights to sources"
```

---

### Task 11: Version bump to 0.18.0 + CHANGELOG

**Files:**

- Modify: `pyproject.toml` (version), `src/data_aggregator_mcp/__init__.py` (`__version__`), `server.json` (×2 version fields), `CHANGELOG.md`, `tests/test_packaging.py` (version test name + value)

- [ ] **Step 1: Update the version test** in `tests/test_packaging.py` — find the test asserting the version is `0.17.0` (named like `test_version_is_0170_and_synced`) and update both the name and the expected value to `0.18.0`:

```python
def test_version_is_0180_and_synced():
    assert _pkg_version() == "0.18.0"
    # ... keep the existing sync assertions across pyproject / __init__ / server.json
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_packaging.py -k version -v`
Expected: FAIL — expected `0.18.0`, got `0.17.0`

- [ ] **Step 3: Bump the version in all four sites**

- `pyproject.toml`: `version = "0.18.0"`
- `src/data_aggregator_mcp/__init__.py`: `__version__ = "0.18.0"`
- `server.json`: both the top-level `version` and the package `version` field → `0.18.0`

- [ ] **Step 4: Add the CHANGELOG section** at the top of `CHANGELOG.md` (under the header, above the 0.17.0 section):

```markdown
## [0.18.0] - 2026-05-31

### Added

- **DataONE** source — eco/environmental federation (KNB, Arctic Data Center, PANGAEA, …) with verified fetch: data objects stream from Member Nodes with per-object MD5/SHA-256 checksum verification.
- **OmicsDI** source — proteomics/metabolomics discovery, restricted to the mass-spec modality repos (PRIDE, MassIVE, MetaboLights, Metabolomics Workbench, GNPS, PeptideAtlas, EGA) not already covered by the omics leg.
- **PRIDE** and **MetaboLights** fetch backends — `omicsdi:pride:*` / `omicsdi:metabolights_dataset:*` records fetch end-to-end over the EBI HTTPS mirror (unverified: no upstream checksum; PRIDE is size-checked). Other OmicsDI repos are discovery-only and fail loud at fetch with a source pointer.

### Notes

- OmicsDI contributes first-page results only (modality post-filtering precludes stable pagination).
- No dedup-ranking change: the existing binary rule already keeps the verified copy on every realistic DOI collision.
```

- [ ] **Step 5: Run the full suite, then commit**

Run: `uv run pytest -q -k "not live" && uv run ruff check src tests`
Expected: all pass, no lint errors

```bash
git add pyproject.toml src/data_aggregator_mcp/__init__.py server.json CHANGELOG.md tests/test_packaging.py
git commit -m "chore: bump to 0.18.0 (DataONE + OmicsDI fetch-breadth wave)"
```

---

## Final verification (after all tasks)

- [ ] **Full offline suite + lint:** `uv run pytest -q -k "not live" && uv run ruff check src tests` → all green.
- [ ] **Live smoke (optional, network):** `DATA_AGGREGATOR_MCP_LIVE=1 uv run pytest -q -k live` → DataONE resolve+fetch verifies a real checksum; PRIDE/MetaboLights/OmicsDI/DataONE live calls succeed.
- [ ] Then use **superpowers:finishing-a-development-branch** to merge `fetch-breadth-wave` → `main` (local, `--no-ff`, v0.18.0 milestone).

## Self-Review notes (author)

- **Spec coverage:** every spec section maps to a task — dataone search/resolve (T3/T4), omicsdi search/resolve (T5/T6), pride/metabolights backends (T1/T2), router registration + the §6 "no dedup change" decision (T7), server allowlist+guard (T8), list_sources (T9), README (T10), version (T11). Real-execution doctrine: each adapter has a `@_live_only` test; DataONE's downloads+verifies a real checksum end-to-end.
- **Type consistency:** `files()` backends return `list[FileEntry]`; search returns `(int, list[DataResource])`; resolve returns `DataResource`. `_MODALITY_REPOS` is referenced identically in T5 (definition + test) and T6/T8 prose. Checksum strings are `"<algo>:<hex>"` matching `fetch.py._hasher`.
- **No new model fields** → the existing `tests/test_output_schema_gate.py` round-trip stays valid (no schema-gate task needed).
