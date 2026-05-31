# P4 — HuggingFace Datasets Source (data-aggregator-mcp)

> **Status:** approved direction (user 2026-05-31: continue to P4). Scoped to **HuggingFace
> datasets only**; C2 Mendeley (OAuth) and C5 misc-sources deferred to their own decisions. Ships
> v0.15.0.

**Goal:** Add HuggingFace Hub _datasets_ as a fifth discovery source — searchable, resolvable,
and fetchable — fitting the existing 4 tools (a new _source_, not a new verb).

**Tech stack:** Python 3, httpx (async), pytest. A new `huggingface.py` adapter mirroring the
`zenodo.py` shape (search/resolve/normalize), registered in the router; resolve routing + fetch
gating + `list_sources` updated.

---

## Background (live API + repo, verified 2026-05-31)

- **Search:** `GET https://huggingface.co/api/datasets?search=<q>&limit=<n>&full=true` → JSON
  list of dataset objects: `{id: "owner/name", author, createdAt, lastModified, sha, tags[],
downloads, gated, private, likes, siblings?}`. License/format/size are encoded in `tags` as
  `license:<x>`, `format:<x>`, `size_categories:<x>`, etc. No `description` field.
- **Resolve:** `GET https://huggingface.co/api/datasets/<id>?full=true` → same object plus
  `siblings: [{rfilename: "path/in/repo"}]` (the file list) and sometimes `cardData` (may be
  empty; `cardData.license` when present). No per-file size or checksum in this endpoint.
- **File download:** `https://huggingface.co/datasets/<id>/resolve/main/<rfilename>` (302→CDN).
- **Pagination:** HF uses **Link-header cursor pagination, not offset/skip** — it cannot drive
  the router's per-source row-offset cursor.
- Repo integration points (verified):
  - `router._ADAPTERS` dict (currently zenodo/datacite/omics/literature); `_select` validates
    names against it; `available_sources()` returns its keys.
  - `router.resolve` routes by id prefix (a chain of `if prefix in X.PREFIXES / startswith`).
  - `server._FETCHABLE_SOURCES` tuple gates `fetch`; `server._SOURCES` is the `list_sources`
    payload (each entry: name/description/filters_supported/fetchable/id_example/...).
- A search adapter's contract (per zenodo/datacite): `async search(client, query, *, size,
offset=0) -> tuple[int, list[DataResource]]` returning COMPACT resources; `async resolve(client,
id) -> DataResource`; module-level `PREFIXES` set when resolve-routed by prefix; `DEFAULT_SIZE`,
  `MAX_SIZE`.

---

## The adapter — `huggingface.py`

- `PREFIXES = {"hf"}`. `DEFAULT_SIZE = 10`, `MAX_SIZE = 50`.
- `id` form: `hf:<owner>/<name>` (the HF dataset id, prefixed).

**`_normalize(d: dict) -> DataResource`:**

- `id = f"hf:{d['id']}"`, `source = "huggingface"`, `kind = "dataset"`.
- `title = d["id"]` (owner/name; HF has no separate title field).
- `creators = [Creator(name=d["author"])]` if `author` else `[]`.
- `year` = `int(createdAt[:4])` when parseable else None.
- `doi = None` (HF datasets generally have none).
- `license` = from `tags` (`next(t.split(":",1)[1] for t in tags if t.startswith("license:"))`),
  else `cardData.license` if present, else None.
- `subjects` = the non-namespaced tags (those without a `<ns>:` prefix) — keep it simple; or all
  tags. (Pick: tags that look like plain keywords; namespaced `x:y` tags dropped from subjects.)
- `files`: in search, `[]` (compact). In resolve, one `FileEntry(name=rfilename,
url=f"https://huggingface.co/datasets/{id}/resolve/main/{rfilename}")` per `siblings` entry
  (size/checksum = None — HF basic API doesn't provide them; fetchable-but-unverified, like GEO
  suppl files). Skip `.gitattributes`.
- `access` = `"restricted"` if `d.get("gated")` else `"open"` (public datasets).

**`async search(client, query, *, size=DEFAULT_SIZE, offset=0)`:**

- For `offset == 0`: `GET .../api/datasets?search=<query>&limit=<min(size,MAX_SIZE)>&full=true`;
  return `(len(results), [compact(_normalize(d)) for d in results])`. (HF gives no total-hits
  count; use the returned length as the "total" — an honest lower bound.)
- For `offset > 0`: return `(0, [])`. **HF's cursor pagination doesn't map to row offsets, so HF
  contributes to page 1 only.** This is safe with the router: on deep pages HF returns nothing
  and does not inflate the `more` signal (its `total` is 0 there), and the empty-merge guard
  terminates pagination when every source is exhausted. Documented limitation.

**`async resolve(client, resource_id)`:**

- Strip the `hf:` prefix → `<owner>/<name>`. `GET .../api/datasets/<id>?full=true`; 404 →
  `NotFoundError`. `_normalize` the body, then attach `files` from `siblings`.

## Router + server wiring

- `router._ADAPTERS`: add `"huggingface": huggingface`. (Now 5 search sources; the fan-out,
  interleave, dedup, and pagination machinery already generalize over `_ADAPTERS`.)
- `router.resolve`: add a branch — `elif prefix in huggingface.PREFIXES: resource = await
huggingface.resolve(client, rid)` (place before the bare-DOI `"/" in rid` branch, since an HF id
  contains `/` after the prefix is stripped — but the prefix check is on `rid.split(":")[0] == "hf"`
  so it routes correctly; ensure the `hf:` branch precedes the `"/" in rid` fallback).
- `server._FETCHABLE_SOURCES`: add `"hf:"` (HF files are downloadable, unverified).
- `server._SOURCES`: add a `huggingface` entry — `filters_supported` =
  `["query", "size", "published_after", "published_before", "kind", "cursor"]` (year/kind filters
  work via the router's post-filter on normalized fields; pagination note: page-1 contribution
  only), `fetchable: True`, `fetchable_notes: "Files downloadable via the HF resolve URL
(unverified — no checksum/size in the API)."`, `id_example: "hf:davidcechak/Arabidopsis_thaliana_DNA_v0"`.

---

## Deferred / out of scope

- **C2 Mendeley** (OAuth-gated) and **C5 misc sources** — own decisions/tiers.
- HF **models/spaces** (only _datasets_).
- HF cursor pagination beyond page 1 (its own follow-up if demand appears).
- Per-file size/checksum (needs `?blobs=true` or the tree API; fetch stays unverified for HF).

---

## Testing

Unit (synthetic httpx `MockTransport`):

- `test_huggingface.py` (new):
  - search: maps the API list → `DataResource`s (id `hf:owner/name`, source, kind=dataset,
    creators=[Creator(author)], year from createdAt, license from a `license:` tag, gated→access).
  - search `offset>0` → `(0, [])` (page-1-only contract).
  - resolve: `siblings` → `files` with the correct resolve URLs; `.gitattributes` skipped; 404 →
    `NotFoundError`.
  - `_normalize` license/tag edge cases (no license tag → None; cardData.license fallback).
- `test_router.py`: `available_sources()` now includes `huggingface`; `resolve` routes an `hf:` id
  to the HF adapter (monkeypatch `huggingface.resolve`); a multi-source search includes HF on
  page 1 and pagination still terminates (HF `(0,[])` on offset>0 doesn't stall).
- `test_server.py`: `_is_fetchable("hf:owner/name")` is True; `list_sources` includes the
  `huggingface` entry with the documented fields.

**Real-execution probe (`DATA_AGGREGATOR_MCP_LIVE=1`):** live HF search returns ≥1 dataset that
normalizes cleanly (well-typed `DataResource`, `id` starts `hf:`); live resolve of a known small
dataset attaches `files` with working resolve URLs (HEAD/GET the first non-.gitattributes file →
2xx/3xx). Gated like the other live probes.

## Version

Bump 0.14.0 → 0.15.0 (4 synced places + `test_packaging` + CHANGELOG).
