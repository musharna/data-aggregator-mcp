# Fetch-Breadth Wave — DataONE + OmicsDI Design

**Date:** 2026-05-31
**Status:** Approved (user sign-off incl. §6 simplification)
**Ships:** v0.18.0
**Predecessor:** v0.17.0 (Phase 1 MCP-citizen + trust + interop). Implements the fetch-breadth bet from `docs/superpowers/plans/2026-05-31-one-stop-shop-master-plan.md` (Tier D) and the round-3 source-coverage research.

## Goal

Add two new discovery sources — **DataONE** (eco/environmental federation, MD5-verified fetch) and **OmicsDI** (proteomics/metabolomics modality gap, discovery) — plus direct **PRIDE** and **MetaboLights** fetch backends, so the server covers a net-new domain _with verified fetch_ and closes the mass-spec modality gap with honest (unverified) end-to-end fetch.

## Architecture

Four new adapter modules following the existing `huggingface.py` / `omics.py` shape, plus edits to the router (search fan-out + resolve routing), the server (fetch allowlist + guard), and the `list_sources` registry. `fetch.py` and `models.py` need **no change** — DataONE rides the existing `md5:`-checksum verification path, and the unverified MS backends ride the existing size-check / no-checksum path.

| New module        | Role                     | Searched?     | Verified fetch?           |
| ----------------- | ------------------------ | ------------- | ------------------------- |
| `dataone.py`      | search + resolve + files | yes (fan-out) | **MD5-verified**          |
| `omicsdi.py`      | search + resolve         | yes (fan-out) | n/a (discovery)           |
| `pride.py`        | `files()` backend only   | no            | unverified (size-checked) |
| `metabolights.py` | `files()` backend only   | no            | unverified                |

`pride`/`metabolights` are fetch backends invoked from `omicsdi.resolve()` to populate `files[]`; they are never searched directly — OmicsDI is the discovery front-end for that modality.

## Locked scope decisions (user, 2026-05-31)

1. **Both** DataONE + OmicsDI in one wave (not split).
2. OmicsDI: discovery + **direct PRIDE + MetaboLights fetch** wired (not discovery-only).
3. OmicsDI adapter **restricts to proteomics/metabolomics repos**, dropping GEO/ArrayExpress/ENA (already covered by the omics leg; accession-keyed so DOI-dedup would miss the duplicates).
4. PRIDE + MetaboLights fetch is wired as **honest unverified fetch** (checksum=none), after live probes showed neither exposes a usable checksum. DataONE remains genuinely MD5-verified.

## Live-probed API shapes (verified this session, 2026-05-31 — cite these in the plan)

- **DataONE CN Solr** `GET https://cn.dataone.org/cn/v2/query/solr/?q=<q>&fl=...&rows=N&wt=json` → `{response:{numFound, docs:[{identifier, title, author, formatId, size, checksum, checksumAlgorithm, dateUploaded, ...}]}}`. `checksumAlgorithm` observed = `MD5`.
- **DataONE package → data objects**: `GET .../query/solr/?q=resourceMap:"<rmap>" AND formatType:DATA&fl=identifier,fileName,size,checksum,checksumAlgorithm` → data-object docs. `checksumAlgorithm` **varies per object** (observed both `MD5` and `SHA256`) — build the FileEntry checksum prefix dynamically from the field; `fetch.py`'s `_hasher` (`hashlib.new`) verifies either.
- **DataONE byte fetch is a two-hop** (CN holds metadata; data bytes live on Member Nodes): `GET https://cn.dataone.org/cn/v2/resolve/{url-encoded-pid}` returns an `ObjectLocationList` XML with one or more `<url>` Member-Node direct-byte URLs. `/cn/v2/object/{pid}` **404s** for MN-only objects, so resolve must extract the first `<url>` and put THAT in `FileEntry.url`. Confirmed live: the MN url streams the bytes and the SHA256 matches the Solr `checksum`.
- **OmicsDI** `GET https://www.omicsdi.org/ws/dataset/search?query=<q>&size=N` → `{count, datasets:[{id, source, title, description}]}`. `source` values like `metabolights_dataset`, `pride`, `peptide_atlas`. Repo list: `GET https://www.omicsdi.org/ws/database/all`.
- **PRIDE v3** `GET https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{PXD}/files` → `[{fileName, fileSizeBytes, checksum(""), publicFileLocations:[{name:"FTP Protocol", value:"ftp://ftp.pride.ebi.ac.uk/..."}]}]`. **`checksum` is empty.** The `ftp://ftp.pride.ebi.ac.uk/` host also serves over **`https://ftp.pride.ebi.ac.uk/`** with range support (206) — rewrite scheme to fetch via httpx.
- **MetaboLights** `GET https://www.ebi.ac.uk/metabolights/ws/studies/{MTBLS}/files?include_raw_data=false` → `{study:[{file, type, status}]}`. **No checksum, no size.** Files served at `https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{MTBLS}/{file}` (HTTPS, 200).

## Component designs

### dataone.py

- `PREFIXES = {"dataone"}`.
- `search(client, query, *, size, offset)`: Solr `q=(<query>) AND formatType:METADATA`, `fl=identifier,title,author,origin,formatId,dateUploaded,datePublished,resourceMap`, `rows=size`, paginate via Solr `start=offset` (true row-space paging — `formatType:METADATA` is a query filter, not a post-filter, so offset stays aligned). Map each doc → `DataResource(id=f"dataone:{identifier}", source="dataone", kind="dataset", title, creators=[Creator(name=c) for c in origin] or [Creator(name=author)], year=<from dateUploaded/datePublished>, last_updated=<dateModified if present>, files=[])` — `origin` is the multivalued all-creators field; fall back to single `author`. Return `(numFound, [compact(...)])`.
- `resolve(client, resource_id)`: fetch the metadata Solr doc by `q=identifier:"<pid>"` (raise `NotFoundError` if no doc). Read its `resourceMap[0]`; if absent, return the record with `files=[]` (metadata-only package). Else Solr-query `q=resourceMap:"<rmap>" AND formatType:DATA&fl=identifier,fileName,size,checksum,checksumAlgorithm` for the data objects. For each, resolve its Member-Node byte URL via a helper `_object_url(client, pid)` that GETs `/cn/v2/resolve/{quote(pid, safe='')}`, parses the `ObjectLocationList` XML (ElementTree; first element whose tag local-name is `url`), and returns that url (`None` on no location → skip the file). Build `FileEntry(name=fileName or pid, url=<mn_url>, size=size, checksum=f"{checksumAlgorithm.lower()}:{checksum}" if checksum and checksumAlgorithm else None)`. Run the per-object `_object_url` hops concurrently with `asyncio.gather`.

### omicsdi.py

- `PREFIXES = {"omicsdi"}`.
- `_MODALITY_REPOS = {"pride", "massive", "metabolights_dataset", "metabolomics_workbench", "gnps", "peptide_atlas", "ega"}` (proteomics/metabolomics only).
- `search(client, query, *, size, offset)`: **page-1-only** — `if offset: return 0, []`, mirroring `huggingface.py`'s precedent. (Rationale: we post-filter the page to `_MODALITY_REPOS`, so the router's offset accounting — which counts records _consumed from the merged stream_ — would be in kept-record space while the OmicsDI API offset is in all-rows space; the two cannot be reconciled without scanning, so we contribute first-page results only.) Query the search endpoint, **filter** `datasets` to `source ∈ _MODALITY_REPOS`, map → `DataResource(id=f"omicsdi:{source}:{id}", source="omicsdi", kind="study", title, description, files=[])`. Return `(len(kept), [compact(r) for r in kept])` — total is the kept count (no pagination), so the router never tries to page OmicsDI forward.
- `resolve(...)`: parse `omicsdi:<source>:<acc>`; re-fetch full record for metadata + a `Link` to the source landing page. Route `files[]`: `pride` → `pride.files(client, acc)`; `metabolights_dataset` → `metabolights.files(client, acc)`; otherwise `files=[]`.

### pride.py

- `files(client, accession) -> list[FileEntry]`: GET the v3 files endpoint; for each entry choose the FTP `publicFileLocations` value, rewrite `ftp://ftp.pride.ebi.ac.uk/` → `https://ftp.pride.ebi.ac.uk/`, return `FileEntry(name=fileName, url=<https>, size=fileSizeBytes, checksum=None)`.

### metabolights.py

- `files(client, accession) -> list[FileEntry]`: GET the studies files endpoint; for each `study[]` entry return `FileEntry(name=file, url=f"https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{accession}/{file}", size=None, checksum=None)`.

## Integration edits

### router.py

- Import `dataone`, `omicsdi`. Add to `_ADAPTERS` in precedence order: keep verified natives first — `{"zenodo", "dataone", "datacite", "omics", "literature", "huggingface", "omicsdi"}` (dataone before datacite so a DOI tie keeps the verified copy; omicsdi last — accession-keyed, low collision).
- Add resolve routing: `prefix in dataone.PREFIXES → dataone.resolve`; `prefix in omicsdi.PREFIXES → omicsdi.resolve`.

### Dedup (§6 — ranking refactor dropped as YAGNI, user-approved)

The live `_dedup` (router.py:85-104) is binary: DOI-keyed, a `datacite:`-prefixed record loses to any non-datacite record, else first-seen wins. Walking the real collisions: DataONE-vs-DataCite → DataONE wins (correct, it's the verified copy); DataONE-vs-Zenodo → first-seen, both verified (fine); OmicsDI → accession-keyed, no DOI, never collides. A per-source ranking system is therefore unnecessary (adding a mode the collisions don't need = causal-vs-bandaid red flag). **Only change: register the new adapters in the precedence-ordered dict.** No `_dedup` code change.

### server.py

- Add `"dataone:"`, `"omicsdi:"` to `_FETCHABLE_SOURCES`.
- New `_ensure_omicsdi_fetchable(fid, resource)`: when an `omicsdi:` id resolved to `files=[]` (a non-PRIDE/MetaboLights repo), raise `FetchNotSupportedError` with a precise pointer to the source repo landing page (mirrors `_ensure_repo_fetchable` for Dryad, server.py:56-65). Call it in the fetch dispatch alongside the existing guards.
- Two new `_SOURCES` entries: `dataone` (layer `archives`, fetchable `True`, MD5, eco/env), `omicsdi` (layer `omics`, fetchable `"per-repo"`, notes: PRIDE/MetaboLights fetchable-unverified, other MS repos discovery-only).

## Error handling

All adapters use the shared `_http.request_json` (retry/backoff, `DataAggregatorError` taxonomy), so per-source transport failures surface in the `errors{}` map — never silently dropped. Unsupported OmicsDI repos fail loud at fetch with a source pointer. DataONE checksum mismatch raises via the existing `fetch.py` path.

## Testing (real-execution doctrine — every adapter gets both)

- **Unit** (`httpx.MockTransport`, fixtures = the JSON shapes probed above): DataONE search-normalize + resolve files-with-md5; OmicsDI search filtered to modality repos (asserts a GEO hit is dropped) + resolve routing to pride/metabolights/empty; PRIDE ftp→https rewrite + size; MetaboLights url construction + checksum/size None.
- **Live** (`DATA_AGGREGATOR_MCP_LIVE=1`): each API hit live; **DataONE downloads one small object and asserts the MD5 verifies end-to-end** through `fetch_files`; PRIDE asserts the rewritten HTTPS URL returns 200/206.
- **Schema gate**: the existing `tests/test_output_schema_gate.py` covers the model round-trip (no new fields added).

## Out of scope (documented; do not re-litigate)

MassIVE / Metabolomics Workbench / GNPS / PeptideAtlas / EGA direct fetch (discovery-only this wave); a per-source dedup ranking system (§6); GEO/ArrayExpress/ENA via OmicsDI; Aspera/Globus transports (HTTPS only); async-fetch-jobs; DataONE write/auth paths; PRIDE/MetaboLights checksum verification (no usable checksum upstream).
