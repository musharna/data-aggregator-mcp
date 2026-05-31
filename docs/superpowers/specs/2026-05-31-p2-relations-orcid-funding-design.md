# P2 — PID Relations + Creator ORCID + Funding (data-aggregator-mcp)

> **Status:** approved design (forks decided by user 2026-05-31: D2 restructures `creators` to
> `Creator` objects; D5 Croissant deferred to its own tier). Ships v0.13.0.

**Goal:** Surface the structured provenance metadata the rich sources already return but we drop
today — related-identifier links (D1), creator ORCID iDs and funding references (D2) — by
extending the normalized `DataResource`.

**Tech stack:** Python 3, httpx (async), pytest. Pure metadata-extraction additions in the
adapter `_normalize` functions + two new model types. No new tools, no graph traversal, no
network calls beyond the existing resolve/search requests.

---

## Background (live code, verified 2026-05-31)

- `DataResource.creators` is `list[str]` (names only). The ONLY consumer is
  `citation.py:48` (`item["author"] = [{"literal": c} for c in r.creators]`).
- Four adapters construct `creators`: `datacite.py:121`, `openaire.py:67`, `pubmed.py:48`,
  `zenodo.py:53`. omics (geo/sra/bioproject) and the DataCite-repo adapters
  (figshare/dryad/osf/dataverse) leave it at the default empty list.
- `Link{rel: str, target_id: str}` already exists; `links` is populated today by the router's
  `described_in` enrichment and is NOT stripped by `compact`.
- Rich metadata available but currently dropped:
  - **DataCite** (`attributes`): `relatedIdentifiers[]` (`{relatedIdentifier, relatedIdentifierType,
relationType}`), `creators[].nameIdentifiers[]` (`{nameIdentifier, nameIdentifierScheme}`,
    scheme `"ORCID"`), `fundingReferences[]` (`{funderName, awardNumber, awardTitle}`).
  - **Zenodo** (`metadata`): `creators[].orcid` (bare iD), `related_identifiers[]`
    (`{identifier, relation, scheme}`), `grants[]` (`{code, title, funder:{name}}`).
- `datacite._normalize` / `zenodo._normalize` are shared by both search and resolve.

---

## Model changes (`models.py`)

```python
class Creator(BaseModel):
    name: str
    orcid: str | None = None  # bare ORCID iD, e.g. "0000-0002-1825-0097" (no URL prefix)


class FundingRef(BaseModel):
    funder: str
    award: str | None = None  # award/grant number, else title


class DataResource(BaseModel):
    ...
    creators: list[Creator] = Field(default_factory=list)   # CHANGED from list[str]
    funding: list[FundingRef] = Field(default_factory=list)  # NEW
    ...
```

This is a **breaking output-shape change** to `creators` (string → object). Acceptable: the
package is pre-1.0 (v0.12.0, Development Status :: 4 - Beta). `funding` is purely additive.

`compact()` is unchanged: it keeps `creators`, `funding`, and `links` (it only drops `files`
and truncates `description`). Confirm by reading `models.py:76` before editing.

---

## D1 — Related-identifier links

Both `_normalize` functions append the source's related identifiers to `links` (so they appear
on resolve AND search, consistent with the existing `described_in` links). **No resolution or
graph traversal** — `target_id` is the related identifier verbatim.

- `relationType` / `relation` is normalized to snake*case lower to match the existing rel style
  (e.g. `IsSupplementTo` → `is_supplement_to`). Use a small helper
  `\_rel(s) = re.sub(r"(?<!^)(?=[A-Z])", "*", s).lower()`(or equivalent) — verify it produces`is_supplement_to`, `is_version_of`, `references`.
- DataCite: `Link(rel=_rel(r["relationType"]), target_id=r["relatedIdentifier"])` for each
  `a.get("relatedIdentifiers") or []` that has both fields.
- Zenodo: `Link(rel=_rel(r["relation"]), target_id=r["identifier"])` for each
  `meta.get("related_identifiers") or []` that has both fields.
- Append after any existing links; do not disturb the router's later `described_in` enrichment.

## D2 — Creator ORCID + funding

**Creators** (each adapter maps its author list to `list[Creator]`):

- DataCite: `name = c.get("name", "")`; `orcid` = the `nameIdentifier` of the first
  `c["nameIdentifiers"]` entry whose `nameIdentifierScheme == "ORCID"` (or whose
  `nameIdentifier` looks like an ORCID), stripped of any `https://orcid.org/` prefix to the bare
  iD. None if absent.
- Zenodo: `Creator(name=c.get("name",""), orcid=c.get("orcid"))` (already bare).
- OpenAIRE: `Creator(name=a["fullName"], orcid=<extract from author pid if present else None>)`
  — keep conservative; None when not obviously present.
- PubMed: `Creator(name=a["name"])` (no ORCID in esummary).

Add a tiny shared `_orcid` normalizer (strip URL prefix, validate the `dddd-dddd-dddd-dddX`
shape; return None on mismatch) — put it in `models.py` or a small helper and reuse, rather than
duplicating the regex in each adapter.

**Funding** (`funding: list[FundingRef]`):

- DataCite: for each `a.get("fundingReferences") or []` →
  `FundingRef(funder=f.get("funderName",""), award=f.get("awardNumber") or f.get("awardTitle"))`,
  skipping entries with no funderName.
- Zenodo: for each `meta.get("grants") or []` →
  `FundingRef(funder=(g.get("funder") or {}).get("name","") , award=g.get("code") or g.get("title"))`,
  skipping entries with no funder name.
- OpenAIRE / PubMed: leave `funding` empty for MVP (note in the adapter; not wired).

**citation.py:** update to `item["author"] = [{"literal": c.name} for c in r.creators]`.

---

## Deferred (explicitly out of P2)

- **D5 Croissant** (MLCommons JSON-LD) — its own spec/branch later.
- Citation-graph traversal / resolving related identifiers into full records (D1 stays
  link-only).
- ORCID/funding for omics, pubmed-funding, openaire-funding — additive later if a source warrants.

---

## Testing

Unit (synthetic fixtures, per existing `tests/` patterns):

- `test_models.py`: `Creator`/`FundingRef` round-trip; `_orcid` strips the URL prefix and
  rejects a malformed iD; `compact` preserves `creators`/`funding`/`links`.
- `test_datacite.py`: a fixture with `relatedIdentifiers` → `links` (snake rel + verbatim
  target); creator `nameIdentifiers` ORCID → `Creator.orcid`; `fundingReferences` → `funding`;
  a creator with no ORCID → `orcid is None`.
- `test_zenodo.py`: creator `orcid` field → `Creator.orcid`; `related_identifiers` → `links`;
  `grants` → `funding`.
- `test_openaire.py` / `test_pubmed.py`: creators become `Creator` objects (orcid None);
  existing assertions updated from `["Name"]` to `[Creator(name="Name")]` (or `.name` access).
- `test_citation.py`: still renders authors from `Creator.name` (update fixture to Creator
  objects).
- `test_router.py` / `test_server.py`: any test asserting on `creators` as strings updated to
  the object shape. The `search`/`resolve` output schemas pick up the new fields automatically.

**Real-execution probe (boundary, `DATA_AGGREGATOR_MCP_LIVE=1`):** resolve a known DataCite DOI
that carries `relatedIdentifiers` + `fundingReferences` and assert `links`/`funding` populated;
resolve/search a Zenodo record with a creator ORCID and assert `creators[i].orcid` is set.

## Version

Bump 0.12.0 → 0.13.0 (4 synced places + `test_packaging` + a `CHANGELOG.md` section).
