# CELLxGENE — single-cell atlas native adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the single-cell modality — **CZ CELLxGENE Discover** as a full native adapter (search/resolve/fetch over the public Discover _curation_ REST API), without expanding the `kind` enum. This is the master plan's resolved-build-order step 4 ("add single-cell atlases CELLxGENE/HCA into the fetch-breadth wave when scoped"). Releases as **v0.23.0**.

**Architecture (collection-as-resource):** CELLxGENE gets a standalone module (`cellxgene.py`) exposing the established `PREFIXES`/`search`/`resolve` contract and slotting into `router._ADAPTERS` + the `router.resolve` elif-chain + `server._SOURCES`/`_FETCHABLE_SOURCES`, exactly like DANDI/PDB. The **collection** is the `DataResource` unit (NOT the dataset): a collection carries a single publication DOI (`collection.doi`, e.g. `10.1038/s41467-...`) and bundles N datasets, each with H5AD/RDS download assets. Modeling the collection as the resource gives a clean paper↔data bridge and avoids DOI-collapse under the cross-source dedup (every dataset in a collection shares one `collection_doi`; per-dataset records would all collapse to a single deduped row). `resolve` flattens every dataset's `assets[]` into `files[]`. `kind="dataset"` (already valid — no enum change).

**Tech Stack:** Python 3.11+, `httpx` (async via `_http.request_json`), `pydantic` v2 models, `pytest`/`pytest-asyncio` with `httpx.MockTransport` for unit tests and `DATA_AGGREGATOR_MCP_LIVE=1`-gated live probes (the pattern in `tests/test_dandi.py` / the P3.5–P3.6 adapters).

---

## Grounding — live API shapes (probed 2026-06-10; fixtures below are trimmed REAL responses)

**CZ CELLxGENE Discover — curation REST API** (public, no auth; base `https://api.cellxgene.cziscience.com/curation/v1`):

- **Search source = the collections list.** `GET /curation/v1/collections` → a bare JSON **array** (NOT a `{count,results}` envelope) of **379** collections (probed), **3.04 MB**. **There is NO server-side search** — `?search=lung` is ignored (returns all). So `search` fetches the full list and filters client-side. Each collection in the list:

  ```json
  {
    "collection_id": "db468083-...",
    "collection_url": "https://cellxgene.cziscience.com/collections/db468083-...",
    "name": "...",
    "description": "...",
    "doi": "10.1038/s41467-020-18957-w",
    "consortia": ["CZI Cell Science"],
    "contact_name": "Sheng Zhong",
    "published_at": "2021-05-06T16:41:21+00:00",
    "revised_at": "2025-10-24T...",
    "visibility": "PUBLIC",
    "publisher_metadata": {
      "authors": [{ "family": "Calandrelli", "given": "Riccardo" }],
      "journal": "Nat Commun",
      "published_year": 2020,
      "is_preprint": false
    },
    "links": [
      {
        "link_name": "GSE135357",
        "link_type": "RAW_DATA",
        "link_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE135357"
      }
    ],
    "datasets": [
      {
        "dataset_id": "9349c6fb-...",
        "tissue": [{ "label": "blood", "ontology_term_id": "UBERON:0000178" }],
        "disease": [{ "label": "normal", "ontology_term_id": "PATO:0000461" }],
        "organism": [
          { "label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606" }
        ],
        "assay": [{ "label": "10x 3' v3", "ontology_term_id": "EFO:0009922" }]
      }
    ]
  }
  ```

  The list **DOES** carry nested `datasets[]` with `tissue`/`disease`/`organism`/`assay` (each a list of `{label, ontology_term_id}`) — enough for biological filtering — but the list **does NOT** carry the datasets' `assets[]` (download URLs). `doi` is **null for 17/379** collections (preprints/unpublished). `link_type` vocab: `RAW_DATA, DATA_SOURCE, LAB_WEBSITE, PROTOCOL, OTHER`. All `visibility:"PUBLIC"`.

- **Resolve source = a single collection.** `GET /curation/v1/collections/{collection_id}` → one collection object (same keys as the list) where each nested dataset now ALSO has `assets[]`:
  ```json
  {"collection_id":"...","doi":"10.1038/s41467-020-18957-w","name":"...","publisher_metadata":{...},
   "links":[...],
   "datasets":[{"dataset_id":"9349c6fb-...","title":"...","tissue":[...],"organism":[...],
     "assets":[{"filesize":421889692,"filetype":"H5AD","url":"https://datasets.cellxgene.cziscience.com/9349c6fb-....h5ad"},
               {"filesize":...,"filetype":"RDS","url":"https://datasets.cellxgene.cziscience.com/9349c6fb-....rds"}]}]}
  ```
  Assets are **direct download URLs** (no redirect) for `H5AD` (AnnData) + `RDS` (Seurat). Assets carry `filesize` but **NO checksum** → **unverified fetch** (real URL, no integrity digest — same posture as DANDI/PDB; documented, not a verified-fetch source).

> Heaviness note: the collections list is 3 MB and there is no pagination, so `search` downloads it on **every** call (including paging past offset 0). v1 fetches per-call for correctness + test-cleanliness; a module-level TTL cache of the list is a documented **follow-up** (deferred to avoid cross-test cache pollution).

**Native adapter contract** (verified live 2026-06-10 in `router.py`/`server.py`/`models.py`):

1. `PREFIXES: set[str]`; `async search(client, query, *, size, offset) -> tuple[int, list[DataResource]]` returning `compact()` records; `async resolve(client, resource_id) -> DataResource` with `files[]`.
2. Register in `router._ADAPTERS` (registration order = merge precedence; native fetch backends precede `datacite` so a DOI collision keeps the fetchable copy) + an `elif prefix in cellxgene.PREFIXES:` branch in the `router.resolve` elif-chain (after the `dandi` branch) + `'cellxgene:<id>'` in the final `else: raise ValueError(...)` message.
3. `server._SOURCES` entry + `server._FETCHABLE_SOURCES` prefix `"cellxgene:"`.
4. `_http.request_json(client, method, url, *, service, params=, data=, content=, headers=, timeout=, max_retries=, not_found_returns=)` — `not_found_returns` is honored ONLY on HTTP 404; the collections endpoint answers 200, so set it to `[]` as a defensive default.
5. `DataResource` fields (live `models.py`): `id, source, kind, title, creators:[Creator(name=,orcid=)], year, description, doi, organism:[str], subjects:[str], license, access, last_updated, files:[FileEntry(name=,size=,mime=,url=,checksum=,source=)], links:[Link(rel=,target_id=)]`. Helpers: `models.compact`, `models.normalize_access`.

---

## File Structure

- `src/data_aggregator_mcp/cellxgene.py` — CELLxGENE native adapter (Task 1).
- `src/data_aggregator_mcp/router.py` — import + `_ADAPTERS` + `resolve` elif branch (Task 1).
- `src/data_aggregator_mcp/server.py` — `_SOURCES`, `_FETCHABLE_SOURCES`, search blurb (Tasks 1, 2).
- `tests/test_cellxgene.py` — unit + gated live tests (Task 1).
- `tests/test_router.py` — `available_sources()` list + precedence assertions + a default-fan-out CELLxGENE mock (Task 1).
- `CHANGELOG.md`, `pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, `server.json`, `tests/test_packaging.py` — release (Task 2).

Build order: **CELLxGENE native adapter → integration/release.** Each task ends green + committed.

---

### Task 1: CELLxGENE native adapter (search + resolve + fetch manifest + register)

**Files:** Create `src/data_aggregator_mcp/cellxgene.py`; modify `router.py`, `server.py`; test `tests/test_cellxgene.py`, `tests/test_router.py`.

Read `src/data_aggregator_mcp/dandi.py` (closest analog: native, fetchable-but-unverified REST) + `pdb.py` first for the house pattern.

- [ ] **Step 1: Write the failing search test → `tests/test_cellxgene.py`**

```python
import os

import httpx
import pytest

from data_aggregator_mcp import cellxgene
from data_aggregator_mcp.errors import NotFoundError

# Trimmed REAL /curation/v1/collections shape (a bare JSON array).
_COLLECTIONS = [
    {
        "collection_id": "col-lung-1",
        "collection_url": "https://cellxgene.cziscience.com/collections/col-lung-1",
        "name": "Human lung cell atlas",
        "description": "An integrated atlas of the human respiratory system.",
        "doi": "10.1038/s41586-020-1111-1",
        "consortia": ["HCA"],
        "published_at": "2021-05-06T16:41:21+00:00",
        "revised_at": "2025-10-24T21:07:43+00:00",
        "visibility": "PUBLIC",
        "publisher_metadata": {
            "authors": [{"family": "Smith", "given": "Jane"}, {"name": "Lung Consortium"}],
            "journal": "Nature",
            "published_year": 2021,
            "is_preprint": False,
        },
        "links": [
            {"link_name": "GSE111", "link_type": "RAW_DATA",
             "link_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE111"},
        ],
        "datasets": [
            {"dataset_id": "ds-1",
             "tissue": [{"label": "lung", "ontology_term_id": "UBERON:0002048"}],
             "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
             "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
             "assay": [{"label": "10x 3' v3", "ontology_term_id": "EFO:0009922"}]},
        ],
    },
    {
        "collection_id": "col-brain-2",
        "collection_url": "https://cellxgene.cziscience.com/collections/col-brain-2",
        "name": "Mouse cortex survey",
        "description": "Single-cell survey of the mouse cortex.",
        "doi": None,  # 17/379 collections have no DOI
        "consortia": [],
        "published_at": "2022-01-02T00:00:00+00:00",
        "revised_at": "2022-02-02T00:00:00+00:00",
        "visibility": "PUBLIC",
        "publisher_metadata": {"authors": [{"family": "Doe", "given": "John"}],
                               "published_year": 2022, "is_preprint": True},
        "links": [],
        "datasets": [
            {"dataset_id": "ds-2",
             "tissue": [{"label": "cortex", "ontology_term_id": "UBERON:0000956"}],
             "disease": [{"label": "normal", "ontology_term_id": "PATO:0000461"}],
             "organism": [{"label": "Mus musculus", "ontology_term_id": "NCBITaxon:10090"}],
             "assay": [{"label": "Smart-seq2", "ontology_term_id": "EFO:0008931"}]},
        ],
    },
]


@pytest.mark.asyncio
async def test_search_filters_collections_on_tissue_and_normalizes():
    async def handler(request):
        assert request.url.path.endswith("/curation/v1/collections")
        return httpx.Response(200, json=_COLLECTIONS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        total, recs = await cellxgene.search(c, "lung", size=10)
    assert total == 1  # only the lung collection's nested tissue matches
    r = recs[0]
    assert r.id == "cellxgene:col-lung-1" and r.source == "cellxgene" and r.kind == "dataset"
    assert r.title == "Human lung cell atlas" and r.year == 2021
    assert r.doi == "10.1038/s41586-020-1111-1"
    assert [cr.name for cr in r.creators] == ["Smith, Jane", "Lung Consortium"]
    assert "Homo sapiens" in r.organism and "lung" in r.subjects
    assert r.files == []  # listing carries no files
    # link mapping: landing_page + the RAW_DATA cross-ref to GEO
    assert any(l.rel == "landing_page" for l in r.links)
    assert any("geo" in l.target_id for l in r.links)


@pytest.mark.asyncio
async def test_search_all_terms_must_match_and_paginates():
    async def handler(request):
        return httpx.Response(200, json=_COLLECTIONS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # multi-term AND: "mouse cortex" matches only the brain collection
        total, recs = await cellxgene.search(c, "mouse cortex", size=10)
        assert total == 1 and recs[0].id == "cellxgene:col-brain-2"
        # offset past the single match → empty window, total still reflects full match count
        total2, recs2 = await cellxgene.search(c, "normal", size=1, offset=1)
        assert total2 == 2 and recs2[0].id == "cellxgene:col-brain-2"  # 2nd of 2 "normal" matches


@pytest.mark.asyncio
async def test_search_non_list_body_is_empty():
    # the default fan-out test mocks every source with `{}` (a dict, not a list);
    # search must coerce non-list bodies to [] instead of iterating dict keys.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        assert await cellxgene.search(c, "x") == (0, [])
```

- [ ] **Step 2: Run → FAIL.** `pytest tests/test_cellxgene.py -k search -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Write `src/data_aggregator_mcp/cellxgene.py` (search half + shared normalize)**

```python
"""CZ CELLxGENE Discover — single-cell datasets, native adapter.

Search/resolve over the public Discover *curation* REST API. The COLLECTION is the
resource unit (a collection carries one publication DOI and bundles N datasets, each
with H5AD/RDS download assets); modeling the collection avoids DOI-collapse under the
cross-source dedup, since every dataset in a collection shares `collection_doi`.

The curation API has NO server-side search, so `search` fetches the full collections
list (a bare JSON array; ~3 MB, no pagination) and filters client-side over each
collection's name/description/consortia/DOI plus its nested datasets' tissue/disease/
organism/assay ontology labels. `resolve` re-fetches the single collection and flattens
every dataset's assets into files[] (download URLs carry filesize but NO checksum →
unverified fetch, like DANDI/PDB). kind="dataset".
"""

from __future__ import annotations

from typing import Any

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import (
    Creator,
    DataResource,
    FileEntry,
    Link,
    compact,
    normalize_access,
)

API = "https://api.cellxgene.cziscience.com/curation/v1"
_LANDING = "https://cellxgene.cziscience.com/collections/{id}"
PREFIXES = {"cellxgene"}
DEFAULT_SIZE = 10
MAX_SIZE = 50
MANIFEST_CAP = 200  # max files surfaced on resolve; huge atlas collections are truncated (documented)
_DESC_CAP = 500  # description truncation
DEFAULT_TIMEOUT = 60.0  # the collections list is ~3 MB
MAX_RETRIES = 2

# nested-dataset label fields rolled into the searchable blob + subjects[].
_LABEL_FIELDS = ("tissue", "disease", "assay")


def _labels(collection: dict, field: str) -> list[str]:
    """Unique ontology labels for `field` across a collection's nested datasets."""
    seen: list[str] = []
    for d in collection.get("datasets") or []:
        for term in d.get(field) or []:
            label = term.get("label") if isinstance(term, dict) else None
            if label and label not in seen:
                seen.append(label)
    return seen


def _subjects(collection: dict) -> list[str]:
    out: list[str] = []
    for field in _LABEL_FIELDS:
        for label in _labels(collection, field):
            if label not in out:
                out.append(label)
    return out


def _searchable(collection: dict) -> str:
    parts: list[str] = [
        collection.get("name") or "",
        collection.get("description") or "",
        collection.get("doi") or "",
    ]
    parts += collection.get("consortia") or []
    parts += _labels(collection, "organism")
    parts += _subjects(collection)
    return " ".join(parts).lower()


def _creators(pm: dict) -> list[Creator]:
    out: list[Creator] = []
    for a in pm.get("authors") or []:
        name = a.get("name") or ", ".join(p for p in (a.get("family"), a.get("given")) if p)
        if name:
            out.append(Creator(name=name))
    return out


def _year(collection: dict, pm: dict) -> int | None:
    y = pm.get("published_year")
    if isinstance(y, int):
        return y
    s = collection.get("published_at") or ""
    return int(s[:4]) if s[:4].isdigit() else None


def _links(collection: dict) -> list[Link]:
    cid = collection.get("collection_id") or ""
    out = [
        Link(
            rel="landing_page",
            target_id=collection.get("collection_url") or _LANDING.format(id=cid),
        )
    ]
    for link in collection.get("links") or []:
        url = link.get("link_url")
        if url:
            out.append(Link(rel=(link.get("link_type") or "related").lower(), target_id=url))
    return out


def _truncate(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _DESC_CAP else text[:_DESC_CAP].rstrip() + "…"


def _normalize(collection: dict) -> DataResource:
    cid = collection.get("collection_id") or ""
    pm = collection.get("publisher_metadata") or {}
    return DataResource(
        id=f"cellxgene:{cid}",
        source="cellxgene",
        kind="dataset",
        title=collection.get("name") or cid,
        creators=_creators(pm),
        year=_year(collection, pm),
        description=_truncate(collection.get("description")),
        doi=collection.get("doi") or None,
        organism=_labels(collection, "organism"),
        subjects=_subjects(collection),
        access=normalize_access("open"),
        last_updated=collection.get("revised_at") or collection.get("published_at") or None,
        files=[],
        links=_links(collection),
    )


async def _collections(client: httpx.AsyncClient) -> list[dict]:
    body: Any = await _http.request_json(
        client,
        "GET",
        f"{API}/collections",
        service="CELLxGENE search",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=[],
    )
    return body if isinstance(body, list) else []


async def search(
    client: httpx.AsyncClient, query: str, *, size: int = DEFAULT_SIZE, offset: int = 0
) -> tuple[int, list[DataResource]]:
    collections = await _collections(client)
    terms = [t for t in query.lower().split() if t]
    matched = [c for c in collections if all(t in _searchable(c) for t in terms)]
    capped = min(size, MAX_SIZE)
    window = matched[offset : offset + capped] if capped else matched
    return len(matched), [compact(_normalize(c)) for c in window]
```

- [ ] **Step 4: Run → PASS.** `pytest tests/test_cellxgene.py -k search -v`.

- [ ] **Step 5: Write the failing resolve test (append to `tests/test_cellxgene.py`)**

```python
_DETAIL = {
    "collection_id": "col-lung-1",
    "collection_url": "https://cellxgene.cziscience.com/collections/col-lung-1",
    "name": "Human lung cell atlas",
    "description": "An integrated atlas.",
    "doi": "10.1038/s41586-020-1111-1",
    "consortia": ["HCA"],
    "published_at": "2021-05-06T16:41:21+00:00",
    "revised_at": "2025-10-24T21:07:43+00:00",
    "publisher_metadata": {"authors": [{"family": "Smith", "given": "Jane"}], "published_year": 2021},
    "links": [],
    "datasets": [
        {"dataset_id": "ds-1", "title": "Lung 10x",
         "organism": [{"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}],
         "tissue": [{"label": "lung", "ontology_term_id": "UBERON:0002048"}],
         "assets": [
             {"filesize": 421889692, "filetype": "H5AD",
              "url": "https://datasets.cellxgene.cziscience.com/ds-1.h5ad"},
             {"filesize": 511111111, "filetype": "RDS",
              "url": "https://datasets.cellxgene.cziscience.com/ds-1.rds"},
             {"filesize": 1, "filetype": "H5AD", "url": None},  # url-less → skipped
         ]},
        {"dataset_id": "ds-2", "title": "Lung Smart-seq", "assets": [
            {"filesize": 222, "filetype": "H5AD",
             "url": "https://datasets.cellxgene.cziscience.com/ds-2.h5ad"}]},
    ],
}


@pytest.mark.asyncio
async def test_resolve_flattens_assets_into_files():
    async def handler(request):
        assert request.url.path.endswith("/curation/v1/collections/col-lung-1")
        return httpx.Response(200, json=_DETAIL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await cellxgene.resolve(c, "cellxgene:col-lung-1")
    assert r.id == "cellxgene:col-lung-1" and r.doi == "10.1038/s41586-020-1111-1"
    assert [f.name for f in r.files] == ["Lung 10x.h5ad", "Lung 10x.rds", "Lung Smart-seq.h5ad"]
    assert r.files[0].url == "https://datasets.cellxgene.cziscience.com/ds-1.h5ad"
    assert r.files[0].size == 421889692 and r.files[0].source == "cellxgene"
    assert all(f.checksum is None for f in r.files)  # unverified — no digest in the API


@pytest.mark.asyncio
async def test_resolve_unknown_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await cellxgene.resolve(c, "cellxgene:does-not-exist")


@pytest.mark.asyncio
async def test_resolve_malformed_id_raises():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(NotFoundError):
            await cellxgene.resolve(c, "cellxgene:")
```

- [ ] **Step 6: Run → FAIL** (`resolve` not defined).

- [ ] **Step 7: Append `resolve` + file manifest to `src/data_aggregator_mcp/cellxgene.py`**

```python
def _file_manifest(collection: dict) -> list[FileEntry]:
    out: list[FileEntry] = []
    for d in collection.get("datasets") or []:
        title = d.get("title") or d.get("dataset_id") or ""
        for a in d.get("assets") or []:
            url = a.get("url")
            if not url:
                continue
            ext = (a.get("filetype") or "").lower()
            name = f"{title}.{ext}" if title and ext else url.rsplit("/", 1)[-1]
            out.append(
                FileEntry(name=name, size=a.get("filesize"), url=url, source="cellxgene")
            )
            if len(out) >= MANIFEST_CAP:
                return out
    return out


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    cid = resource_id.split(":", 1)[1].strip() if ":" in resource_id else ""
    if not cid:
        raise NotFoundError(f"malformed CELLxGENE id {resource_id!r}")
    collection = await _http.request_json(
        client,
        "GET",
        f"{API}/collections/{cid}",
        service="CELLxGENE resolve",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if not collection or not collection.get("collection_id"):
        raise NotFoundError(f"CELLxGENE has no collection {cid}")
    record = _normalize(collection)
    record.files = _file_manifest(collection)
    return record
```

- [ ] **Step 8: Run → PASS.** `pytest tests/test_cellxgene.py -k resolve -v`.

- [ ] **Step 9: Register in `src/data_aggregator_mcp/router.py`**
- Add `cellxgene` to the `from data_aggregator_mcp import (...)` block (alphabetical: after `_cursor`, before `dandi`).
- Add `"cellxgene": cellxgene,` to `_ADAPTERS` — place it among the native fetch backends, BEFORE `"datacite"` (registration order = merge precedence; keeps the fetchable copy on a DOI collision). E.g. right after `"dataone": dataone,`.
- In `router.resolve`, after the `elif prefix in dandi.PREFIXES:` branch, add:

```python
    elif prefix in cellxgene.PREFIXES:
        resource = await cellxgene.resolve(client, rid)
```

- Add `'cellxgene:<id>'` to the final `else: raise ValueError(...)` message string (alongside the existing examples).

- [ ] **Step 10: Register in `src/data_aggregator_mcp/server.py`**
- Add `"cellxgene:",` to `_FETCHABLE_SOURCES`.
- Append to `_SOURCES`:

```python
    {
        "name": "cellxgene",
        "layer": "omics",
        "kinds": ["dataset"],
        "filters_supported": ["query", "size"],
        "auth_required": False,
        "rate_limit": "public; courtesy only",
        "status": "live (CZ CELLxGENE Discover collections search/resolve; asset manifest on resolve)",
        "fetchable": True,
        "operable": False,
        "fetchable_notes": "H5AD/RDS assets stream from datasets.cellxgene.cziscience.com (direct URLs, unverified — no checksum in the API); the per-collection manifest is capped at 200 files for large atlases.",
        "id_example": "cellxgene:col-lung-1",
        "description": "CZ CELLxGENE Discover — single-cell datasets grouped by collection (one publication DOI per collection); search filters on tissue/disease/organism/assay, resolve attaches the H5AD/RDS download manifest.",
    },
```

- [ ] **Step 11: Append a registration test to `tests/test_cellxgene.py`**

```python
def test_registered_in_router_and_server():
    from data_aggregator_mcp import router, server

    assert "cellxgene" in router.available_sources()
    assert router._ADAPTERS["cellxgene"] is cellxgene
    # native backend precedes datacite in merge precedence
    names = list(router._ADAPTERS)
    assert names.index("cellxgene") < names.index("datacite")
    assert "cellxgene:" in server._FETCHABLE_SOURCES
    assert any(s["name"] == "cellxgene" for s in server._SOURCES)
```

- [ ] **Step 12: Append the gated live probe**

```python
_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"
_live_only = pytest.mark.skipif(not _LIVE, reason="set DATA_AGGREGATOR_MCP_LIVE=1 to run")


@_live_only
@pytest.mark.asyncio
async def test_live_search_then_resolve():
    async with httpx.AsyncClient(timeout=120) as c:
        total, recs = await cellxgene.search(c, "lung", size=3)
        assert total > 0 and recs and recs[0].id.startswith("cellxgene:")
        full = await cellxgene.resolve(c, recs[0].id)
        assert full.kind == "dataset" and full.files  # asset manifest attached
        assert all(f.url and f.url.startswith("https://datasets.cellxgene") for f in full.files)
```

- [ ] **Step 13: Update `tests/test_router.py` for the new default fan-out adapter**

CELLxGENE becomes a default fan-out adapter, so the registry-list + precedence assertions and the "mock every default source" tests change (as in P3.5/P3.6):

- `test_available_sources_lists_all_adapters` (the explicit `==` list assertion): insert `"cellxgene"` in the position matching `_ADAPTERS` order (after `dataone`, before `datacite`).
- Any precedence/registry test (e.g. `test_dataone_and_omicsdi_registered_in_precedence_order`): if it asserts neighbours, keep it consistent with the new order.
- The default-fan-out search test(s) that drive every adapter: the generic `MockTransport(lambda r: Response(200, json={}))` handler already covers CELLxGENE (the `isinstance(body, list)` guard turns `{}` into `(0, [])`). If a test asserts an exact aggregate result set, confirm CELLxGENE contributes zero rows under that mock (it will). Add a CELLxGENE-specific mock branch ONLY if an existing test routes by URL and would 404 otherwise.

- [ ] **Step 14: Verify green + gates**

```
pytest tests/test_cellxgene.py tests/test_router.py tests/test_server.py tests/test_output_schema_gate.py -q
ruff check src tests && ruff format --check src tests && mypy src
```

All gates must pass; do NOT run the live test. Fix any mypy `None`-narrowing with explicit guards (house style; no `# type: ignore`). The `_collections` return is typed `list[dict]` via the `isinstance` guard so the comprehensions stay clean.

- [ ] **Step 15: Commit**

```
git add src/data_aggregator_mcp/cellxgene.py src/data_aggregator_mcp/router.py src/data_aggregator_mcp/server.py tests/test_cellxgene.py tests/test_router.py
git commit -m "feat(cellxgene): add CZ CELLxGENE Discover source adapter (collection search + resolve + asset manifest)"
```

---

### Task 2: Integration polish + release (v0.23.0)

**Files:** `server.py` (search blurb), `CHANGELOG.md`, `pyproject.toml`, `src/data_aggregator_mcp/__init__.py`, `server.json`, `tests/test_packaging.py`.

- [ ] **Step 1: Update the `search` tool description in `server.py`** — add CELLxGENE to the fan-out source list. Find the sentence ending `..., OpenML (ML datasets), and DANDI (neurophysiology dandisets).` and change it so CELLxGENE is included, e.g.:
      `..., OpenML (ML datasets), DANDI (neurophysiology dandisets), and CZ CELLxGENE (single-cell datasets).`

- [ ] **Step 2: Full suite + gates**

```
pytest -q && ruff check src tests && ruff format --check src tests && mypy src
```

Expected all green (live tests skipped).

- [ ] **Step 3: Live probe (real-execution check)**

```
DATA_AGGREGATOR_MCP_LIVE=1 pytest tests/test_cellxgene.py -k live -v
```

Expected: PASS against the real CELLxGENE Discover curation API. If the endpoint is transiently slow (3 MB list), re-run; do not mark green on a network error.

- [ ] **Step 4: Update `CHANGELOG.md`** — add a new release section above the latest (`## [0.22.0]`), keeping a single empty `## [Unreleased]` at the top:

```markdown
## [Unreleased]

## [0.23.0] - 2026-06-10

### Added

- **CZ CELLxGENE Discover** source — single-cell datasets via the Discover curation
  REST API. The collection is the resource unit (one publication DOI per collection);
  search filters client-side on each collection's tissue/disease/organism/assay
  ontology labels, and `resolve` attaches the H5AD/RDS download manifest (capped at
  200 files; direct URLs, unverified — the API exposes filesize but no checksum).
  `kind="dataset"`.
```

- [ ] **Step 5: Bump the version to 0.23.0 in ALL FOUR places + the packaging test** (a CI packaging test pins the version literal):
  - `pyproject.toml`: `version = "0.23.0"`
  - `src/data_aggregator_mcp/__init__.py`: `__version__ = "0.23.0"`
  - `server.json`: both `"version": "0.22.0"` occurrences (top-level + `packages[0]`) → `"0.23.0"`
  - `tests/test_packaging.py`: rename `test_version_is_0220_and_synced` → `test_version_is_0230_and_synced`, and update both `"0.22.0"` literals (in that test and in `test_server_json_matches_package_identity`) to `"0.23.0"`. (Grep first: `grep -rn "0.22.0" tests/test_packaging.py`.)

- [ ] **Step 6: Final gate run with the operate extra (mirrors CI's coverage gate exactly)**

```
pip install -q "duckdb>=1.1" "pyarrow>=17" "fsspec>=2024.6"   # if not already installed
pytest -q --cov=data_aggregator_mcp --cov-report=term-missing --cov-fail-under=92
```

Expected: coverage ≥ 92% (P3.6 left the suite at ~95%). If under 92%, add unit tests for uncovered branches in `cellxgene.py` (e.g. the `not_found_returns`/empty-list paths, manifest cap) before committing.

- [ ] **Step 7: Commit**

```
git add src/data_aggregator_mcp/server.py CHANGELOG.md pyproject.toml src/data_aggregator_mcp/__init__.py server.json tests/test_packaging.py
git commit -m "feat: CELLxGENE single-cell breadth (v0.23.0)"
```

- [ ] **Step 8: Release (ONLY after the user confirms)** — merge to `main`, push, then create the GitHub Release (publish triggers on `release: published`, NOT the tag). Verify `origin/main` is an ancestor first (`git merge --ff-only` else rebase):

```
git checkout main && git merge --ff-only <branch> && git push origin main
git tag v0.23.0 && git push origin v0.23.0    # trailer-free lightweight tag
gh release create v0.23.0 --title "v0.23.0 — CZ CELLxGENE (single-cell breadth)" --notes "<CHANGELOG 0.23.0 section>"
```

Then watch `gh run list` for **Publish** + **Publish to MCP Registry** to go green, and confirm PyPI serves 0.23.0.

---

## Self-Review

**1. Spec coverage (single-cell atlas breadth; collection-as-resource):**

- Native search/resolve/fetch over the CELLxGENE Discover curation API → Task 1 (client-side collection filter on tissue/disease/organism/assay, single-collection resolve with DOI + creators + manifest, registered as a fetchable adapter). ✓
- Collection is the resource unit (one DOI per collection) → avoids DOI-collapse under cross-source dedup; paper↔data bridge via `collection.doi` + GEO/HCA cross-refs in `links[]`. ✓
- No `kind`-enum expansion (`dataset`). ✓
- Master-plan "each new source searches + resolves + (where fetchable) fetches" → all three; ToS: CELLxGENE Discover is public CC-BY single-cell data, not on the AVOID list. ✓ Honest-fetch posture: assets are unverified (no checksum) and the `_SOURCES`/CHANGELOG/notes say so explicitly (no false "verified" claim). ✓

**2. Placeholder scan:** every step has concrete code or an exact command; error paths (malformed id, 404, non-list body, url-less asset, manifest cap, DOI-null collection) have explicit tests + guards. No TBD/"handle edge cases". ✓

**3. Type consistency:** exposes the standard `search(...)->tuple[int,list[DataResource]]` + `resolve(...)->DataResource` + `PREFIXES`. `FileEntry`/`Creator`/`Link` field names (`target_id`, `source`, `filesize→size`) match the live model. `_http.request_json` called with verified kwargs (no `json=`); `not_found_returns` set as a 404-only defensive default. `_collections` returns `list[dict]` via the `isinstance` guard so the comprehensions + `mypy src` stay clean. All helpers (`_labels`/`_subjects`/`_searchable`/`_creators`/`_year`/`_links`/`_truncate`/`_normalize`/`_file_manifest`) defined before use. ✓

**4. Known v1 limitations (documented, not bugs):** (a) `search` downloads the full 3 MB collections list per call — a module-level TTL cache is a deferred follow-up; (b) assets are unverified (no checksum in the API); (c) the manifest is capped at 200 files for very large atlas collections. All surfaced in the docstring + `_SOURCES.fetchable_notes` + CHANGELOG.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-10-cellxgene-single-cell.md`. Two execution options:

1. **Subagent-Driven (recommended)** — one fresh subagent per task (1–2), two-stage review (spec then quality) between tasks.
2. **Inline Execution** — execute in this session via `superpowers:executing-plans` with checkpoints.
