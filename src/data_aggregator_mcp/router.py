"""Multi-source router for the archives layer.

Fans out adapter ``search`` coroutines in parallel, normalizes into one
``DataResource`` stream, dedups by DOI (native fetch backends win over
DataCite metadata), and routes ``resolve`` by id prefix. Per-source failures
are captured into an errors map and surfaced — never silently swallowed (a
dropped adapter would make the model conclude "no data exists").
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from typing import Any

import httpx

from data_aggregator_mcp import (
    _cursor,
    anatomy,
    cellxgene,
    chemistry,
    dandi,
    datacite,
    dataone,
    embeddings,
    gwas,
    huggingface,
    literature,
    mesh,
    omics,
    omicsdi,
    openml,
    operate,
    pdb,
    taxonomy,
    zenodo,
)
from data_aggregator_mcp import assay as assay_mod
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import (
    AssayExpansion,
    ChemicalExpansion,
    DataResource,
    Link,
    MeshExpansion,
    Mirror,
    SearchResult,
    Taxon,
    TaxonExpansion,
    TissueExpansion,
    derive_access_modes,
    derive_version_status,
)

_VALID_KINDS = {"dataset", "sequencing_run", "study", "publication", "software"}

logger = logging.getLogger(__name__)

# Registration order = merge precedence: native fetch backends first so that on
# a DOI collision the fetchable record is encountered before the DataCite one.
_ADAPTERS: dict[str, Any] = {
    "zenodo": zenodo,
    "dataone": dataone,
    "cellxgene": cellxgene,
    "datacite": datacite,
    "dandi": dandi,
    "omics": omics,
    "literature": literature,
    "huggingface": huggingface,
    "omicsdi": omicsdi,
    "openml": openml,
    "pdb": pdb,
    "gwas": gwas,
}


def _cache_ttl() -> float:
    raw = os.environ.get("CACHE_TTL_SECONDS")
    if raw is None:
        return 3600.0
    try:
        return float(raw)
    except ValueError:
        return 3600.0


_RESOLVE_CACHE = TTLCache(maxsize=512, ttl=_cache_ttl())


def available_sources() -> list[str]:
    return list(_ADAPTERS)


def _select(sources: list[str] | None) -> dict[str, Any]:
    if sources is None:
        return dict(_ADAPTERS)
    selected: dict[str, Any] = {}
    for name in sources:
        if name not in _ADAPTERS:
            raise ValueError(f"unknown source {name!r}; available: {', '.join(_ADAPTERS)}")
        selected[name] = _ADAPTERS[name]
    return selected


def _dedup(resources: list[DataResource]) -> list[DataResource]:
    """Dedup by lowercased DOI, preserving first-seen order. On collision, a
    native record (id not prefixed ``datacite:``) replaces a DataCite one so
    the fetchable copy survives. Records without a DOI are always kept.
    """
    by_doi: dict[str, DataResource] = {}
    order: list[str] = []
    no_doi: list[DataResource] = []
    for r in resources:
        if not r.doi:
            no_doi.append(r)
            continue
        key = r.doi.lower()
        existing = by_doi.get(key)
        if existing is None:
            by_doi[key] = r
            order.append(key)
        elif existing.id.startswith("datacite:") and not r.id.startswith("datacite:"):
            by_doi[key] = r
    return [by_doi[k] for k in order] + no_doi


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Compared for EXACT
    normalized equality (never substring, never fuzzy) — the conservative
    content-dedup title key."""
    lowered = _PUNCT_RE.sub(" ", title.lower())
    return _WS_RE.sub(" ", lowered).strip()


def _first_author_surname(r: DataResource) -> str | None:
    """Lowercased last whitespace token of the first creator's name, or None if
    the record has no creators (then the title+author+year path cannot fire)."""
    if not r.creators:
        return None
    name = r.creators[0].name.strip()
    if not name:
        return None
    return name.split()[-1].lower()


def _fingerprint_key(r: DataResource) -> tuple[str, str, int] | None:
    """``(normalized_title, first_author_surname, year)`` ONLY when all three are
    present/non-empty; else None (so a missing field can never satisfy the title
    path). Conservative content-identity key."""
    title = _normalize_title(r.title) if r.title else ""
    surname = _first_author_surname(r)
    if not title or not surname or r.year is None:
        return None
    return (title, surname, r.year)


def _checksums(r: DataResource) -> set[str]:
    """Full ``algo:hex`` checksum strings present on a record's files (byte-level
    identity signal)."""
    return {f.checksum for f in r.files if f.checksum}


def _survivor_rank(r: DataResource) -> tuple[int, int]:
    """Lower sorts first = better survivor. DOI-bearing beats DOI-less; among
    DOI-bearing, a native id (not ``datacite:``-prefixed) beats a ``datacite:``
    one — same precedence spirit as ``_dedup``. Ties fall through to first-seen
    order (stable sort on the group's encounter order)."""
    has_doi = 0 if r.doi else 1
    is_datacite = 1 if r.id.startswith("datacite:") else 0
    return (has_doi, is_datacite)


def _collapse_mirrors(records: list[DataResource]) -> list[DataResource]:
    """Conservative, PURE content-dedup ON TOP OF exact-DOI dedup. Groups records
    that are the SAME dataset under different/no DOIs (a cross-repo mirror), folds
    each group to one survivor, and annotates the survivor's ``mirrors[]`` with the
    other members.

    A record joins a group iff it shares ANY full ``algo:hex`` file checksum with a
    member (byte-identical → definitional identity, source-agnostic) OR has the same
    ``_fingerprint_key`` (normalized-title + first-author-surname + year, all present)
    as a member AND comes from a DIFFERENT source than every member already in that
    group. Title-only or partial matches never merge.

    The CROSS-SOURCE requirement on the fingerprint path is load-bearing: B7 is
    *cross-repo* dedup. Two same-source records that share title+author+year are
    almost always VERSION SIBLINGS (e.g. Zenodo record v1/v2), a relationship already
    modeled by ``is_latest``/``superseded_by`` (B1) — folding them as "mirrors" would
    be wrong. Only a copy in a DIFFERENT repository is a mirror. (Byte-identical
    checksums still fold regardless of source: identical bytes are the same data, and
    version siblings differ in bytes so they do not collide on the checksum path.)

    Survivor selection is deterministic (``_survivor_rank`` + first-seen order). The
    survivor's ``mirrors`` lists every OTHER group member as ``Mirror(source,id,doi)``;
    a record is never its own mirror. First-seen order of survivors is preserved.
    Deterministic, no I/O.
    """

    class _Group:
        __slots__ = ("members", "keys", "checksums", "sources", "order")

        def __init__(self, order: int) -> None:
            self.members: list[DataResource] = []
            self.keys: set[tuple[str, str, int]] = set()
            self.checksums: set[str] = set()
            self.sources: set[str] = set()
            self.order = order

    groups: list[_Group] = []
    for r in records:
        key = _fingerprint_key(r)
        sums = _checksums(r)
        target: _Group | None = None
        for g in groups:
            checksum_hit = bool(sums & g.checksums)
            # Fingerprint match only counts CROSS-source — a same-source title+author+
            # year match is a version sibling (B1's domain), not a cross-repo mirror.
            fingerprint_hit = key is not None and key in g.keys and r.source not in g.sources
            if checksum_hit or fingerprint_hit:
                target = g
                break
        if target is None:
            target = _Group(len(groups))
            groups.append(target)
        target.members.append(r)
        if key is not None:
            target.keys.add(key)
        target.checksums |= sums
        target.sources.add(r.source)

    out: list[DataResource] = []
    for g in groups:
        if len(g.members) == 1:
            out.append(g.members[0])
            continue
        # Stable pick: best rank wins, first-seen order breaks ties.
        survivor = min(enumerate(g.members), key=lambda im: (_survivor_rank(im[1]), im[0]))[1]
        mirrors = [
            Mirror(source=m.source, id=m.id, doi=m.doi) for m in g.members if m is not survivor
        ]
        out.append(survivor.model_copy(update={"mirrors": mirrors}))
    return out


def _or_group(terms: list[str]) -> str:
    """Build a quoted ``"a" OR "b"`` group for query expansion, neutralizing any
    embedded double-quote in a term. Free-text ontology labels (NCBI Taxonomy
    synonyms, MeSH entry terms) must not break the surrounding quoting handed to
    downstream adapters. Terms that are empty after neutralization are dropped.
    Shared by ``_expand_organism`` and ``_expand_disease`` so the safety lives in
    one place."""
    safe = [t.replace('"', " ").strip() for t in terms]
    return " OR ".join(f'"{t}"' for t in safe if t)


async def _expand_organism(
    client: httpx.AsyncClient, query: str, organism: str | None, errors: dict[str, str]
) -> tuple[str, TaxonExpansion | None]:
    """If ``organism`` resolves, AND ``query`` with a (canonical OR synonyms)
    group and return the echo. A taxonomy lookup failure is recorded in
    ``errors['taxonomy']`` and the query is returned un-expanded (fail-loud:
    the caller sees expansion did not happen, never a silent 'no synonyms').
    """
    if not organism or not organism.strip():
        return query, None
    try:
        info = await taxonomy.resolve_taxon(client, organism)
    except Exception as exc:  # surfaced, not swallowed
        errors["taxonomy"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical_name, *info.synonyms]))
    or_group = _or_group(terms)
    effective = f"({query}) AND ({or_group})"
    expansion = TaxonExpansion(
        input=organism,
        taxid=info.taxid,
        canonical_name=info.canonical_name,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def _expand_disease(
    client: httpx.AsyncClient, query: str, disease: str | None, errors: dict[str, str]
) -> tuple[str, MeshExpansion | None]:
    """If ``disease`` resolves to a MeSH descriptor, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A MeSH lookup failure is
    recorded in ``errors['mesh']`` and the query is returned un-expanded
    (fail-loud — exactly like ``_expand_organism``; this is a search-input
    expansion, NOT a fail-soft resolve enricher).
    """
    if not disease or not disease.strip():
        return query, None
    try:
        info = await mesh.resolve_mesh(client, disease)
    except Exception as exc:  # surfaced, not swallowed
        errors["mesh"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    or_group = _or_group(terms)
    effective = f"({query}) AND ({or_group})"
    expansion = MeshExpansion(
        input=disease,
        mesh_ui=info.ui,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def _expand_tissue(
    client: httpx.AsyncClient, query: str, tissue: str | None, errors: dict[str, str]
) -> tuple[str, TissueExpansion | None]:
    """If ``tissue`` resolves to a UBERON term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A UBERON (EBI OLS) lookup
    failure is recorded in ``errors['uberon']`` and the query is returned
    un-expanded (fail-loud — exactly like ``_expand_organism``/``_expand_disease``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not tissue or not tissue.strip():
        return query, None
    try:
        info = await anatomy.resolve_uberon(client, tissue)
    except Exception as exc:  # surfaced, not swallowed
        errors["uberon"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    or_group = _or_group(terms)
    effective = f"({query}) AND ({or_group})"
    expansion = TissueExpansion(
        input=tissue,
        uberon_id=info.uberon_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def _expand_chemical(
    client: httpx.AsyncClient, query: str, chemical: str | None, errors: dict[str, str]
) -> tuple[str, ChemicalExpansion | None]:
    """If ``chemical`` resolves to a ChEBI term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. A ChEBI (EBI OLS) lookup
    failure is recorded in ``errors['chebi']`` and the query is returned
    un-expanded (fail-loud — exactly like ``_expand_organism``/``_expand_tissue``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not chemical or not chemical.strip():
        return query, None
    try:
        info = await chemistry.resolve_chebi(client, chemical)
    except Exception as exc:  # surfaced, not swallowed
        errors["chebi"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    or_group = _or_group(terms)
    effective = f"({query}) AND ({or_group})"
    expansion = ChemicalExpansion(
        input=chemical,
        chebi_id=info.chebi_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def _expand_assay(
    client: httpx.AsyncClient, query: str, assay: str | None, errors: dict[str, str]
) -> tuple[str, AssayExpansion | None]:
    """If ``assay`` resolves to an EDAM-topic term, AND ``query`` with a
    (canonical OR synonyms) group and return the echo. An EDAM (EBI OLS) lookup
    failure is recorded in ``errors['edam']`` and the query is returned
    un-expanded (fail-loud — exactly like ``_expand_organism``/``_expand_tissue``;
    this is a search-input expansion, NOT a fail-soft resolve enricher). A
    *no-match* is not an error: the query is returned un-expanded with nothing
    recorded.
    """
    if not assay or not assay.strip():
        return query, None
    try:
        info = await assay_mod.resolve_edam(client, assay)
    except Exception as exc:  # surfaced, not swallowed
        errors["edam"] = f"{type(exc).__name__}: {exc}"
        return query, None
    if info is None:
        return query, None
    terms = list(dict.fromkeys([info.canonical, *info.synonyms]))
    or_group = _or_group(terms)
    effective = f"({query}) AND ({or_group})"
    expansion = AssayExpansion(
        input=assay,
        edam_id=info.edam_id,
        canonical_name=info.canonical,
        synonyms=list(info.synonyms),
    )
    return effective, expansion


async def _enrich_resource(client: httpx.AsyncClient, r: DataResource) -> DataResource:
    """Normalize ``r.organism`` → ``r.taxa`` (taxid+canonical) and append a
    ``described_in`` cross-link to plant-genomics-mcp for Viridiplantae taxa.
    Returns ``r`` unchanged when nothing resolved. May raise (caller handles).
    """
    taxa = list(r.taxa)
    links = list(r.links)
    seen_taxids = {t.taxid for t in taxa}
    seen_links = {lnk.target_id for lnk in links}
    changed = False
    for name in dict.fromkeys(r.organism):  # distinct, order-preserving
        info = await taxonomy.resolve_taxon(client, name)
        if info is None:
            continue
        if info.taxid not in seen_taxids:
            taxa.append(Taxon(taxid=info.taxid, name=info.canonical_name))
            seen_taxids.add(info.taxid)
            changed = True
        if info.is_plant:
            target = f"plant-genomics:taxid:{info.taxid}"
            if target not in seen_links:
                links.append(Link(rel="described_in", target_id=target))
                seen_links.add(target)
                changed = True
    return r.model_copy(update={"taxa": taxa, "links": links}) if changed else r


async def _enrich(
    client: httpx.AsyncClient, resources: list[DataResource], errors: dict[str, str]
) -> list[DataResource]:
    """Enrich each resource with an organism. A taxonomy failure is recorded in
    ``errors['taxonomy']`` and aborts further enrichment (don't hammer a down
    NCBI); already-fetched results are still returned.

    Sequential by design: size<=50 and resolve_taxon is cached, so per-resource
    fan-out isn't worth the complexity.
    """
    out: list[DataResource] = []
    aborted = False
    for r in resources:
        if aborted or not r.organism:
            out.append(r)
            continue
        try:
            out.append(await _enrich_resource(client, r))
        except Exception as exc:  # surfaced, not swallowed
            errors.setdefault("taxonomy", f"{type(exc).__name__}: {exc}")
            aborted = True
            out.append(r)
    return out


def _passes_filters(r: DataResource, f: dict[str, Any]) -> bool:
    """Apply the E2 facet filters to a normalized resource. A record with
    ``year is None`` is dropped whenever either year bound is set (cannot prove
    it satisfies the bound — fail toward exclusion).
    """
    pa, pb, kind = f.get("published_after"), f.get("published_before"), f.get("kind")
    if kind is not None and r.kind != kind:
        return False
    if (pa is not None or pb is not None) and r.year is None:
        return False
    if pa is not None and r.year < pa:
        return False
    if pb is not None and r.year > pb:  # noqa: SIM103 — parallel guard-clause style
        return False
    return True


def _with_version_status(r: DataResource) -> DataResource:
    is_latest, superseded_by = derive_version_status(r.links)
    if is_latest is None and superseded_by is None:
        return r
    return r.model_copy(update={"is_latest": is_latest, "superseded_by": superseded_by})


async def search_page(
    client: httpx.AsyncClient,
    *,
    query: str | None = None,
    size: int = 10,
    sources: list[str] | None = None,
    organism: str | None = None,
    disease: str | None = None,
    tissue: str | None = None,
    chemical: str | None = None,
    assay: str | None = None,
    published_after: int | None = None,
    published_before: int | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    rank: str = "relevance",
    collapse_mirrors: bool = False,
) -> SearchResult:
    """Fan out a search, merge + dedup, filter, and walk to a cut point that
    advances per-adapter offsets — returning a ``SearchResult`` whose
    ``next_cursor`` replays the next page.

    Two call modes: a fresh search (pass ``query`` + optional
    ``sources``/``organism``/filters/``size``) or a continuation (pass only
    ``cursor``; every other parameter is read from the cursor and the organism
    is NOT re-expanded, keeping pages consistent). See the pagination spec for
    the cut-point offset-advance that prevents a fully-filtered page stalling.
    """
    if kind is not None and kind not in _VALID_KINDS:
        raise ValidationError(f"unknown kind {kind!r}; valid: {sorted(_VALID_KINDS)}")

    if cursor is not None:
        st = _cursor.decode(cursor)
        query = st["q"]
        sources = st.get("sources")
        organism = st.get("organism")
        filters = st.get("filters") or {}
        size = st["size"]
        offsets = st["offsets"]
        rank = st.get("rank", "relevance")
        disease = st.get("disease")
        tissue = st.get("tissue")
        chemical = st.get("chemical")
        assay = st.get("assay")
        collapse_mirrors = st.get("collapse_mirrors", False)
        expansion = None  # frozen on continuation; do not re-expand
        disease_expansion = None  # frozen on continuation; do not re-expand
        tissue_expansion = None  # frozen on continuation; do not re-expand
        chemical_expansion = None  # frozen on continuation; do not re-expand
        assay_expansion = None  # frozen on continuation; do not re-expand
        effective_query = query
        errors: dict[str, str] = {}
    else:
        if query is None:
            raise ValidationError("search requires either 'query' or 'cursor'")
        filters = {
            "published_after": published_after,
            "published_before": published_before,
            "kind": kind,
        }
        errors = {}
        effective_query, expansion = await _expand_organism(client, query, organism, errors)
        effective_query, disease_expansion = await _expand_disease(
            client, effective_query, disease, errors
        )
        effective_query, tissue_expansion = await _expand_tissue(
            client, effective_query, tissue, errors
        )
        effective_query, chemical_expansion = await _expand_chemical(
            client, effective_query, chemical, errors
        )
        effective_query, assay_expansion = await _expand_assay(
            client, effective_query, assay, errors
        )
        offsets = {}

    adapters = _select(sources)
    names = list(adapters)
    outcomes = await asyncio.gather(
        *(
            adapters[n].search(client, effective_query, size=size, offset=offsets.get(n, 0))
            for n in names
        ),
        return_exceptions=True,
    )

    origin: dict[int, str] = {}
    per_source: list[list[DataResource]] = []
    totals: dict[str, int] = {}
    total = 0
    for name, outcome in zip(names, outcomes, strict=False):
        if isinstance(outcome, Exception):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            totals[name] = 0
            continue
        # gather(return_exceptions=True) types outcome as tuple | BaseException; the
        # Exception guard above can't subtract the BaseException supertype, so narrow
        # positively to the success tuple before unpacking.
        assert isinstance(outcome, tuple)
        adapter_total, recs = outcome
        total += adapter_total
        totals[name] = adapter_total
        for r in recs:
            origin[id(r)] = name
        per_source.append(recs)

    merged = _dedup(interleave(per_source))

    if rank == "semantic":
        # Re-rank the full fetched window by semantic similarity, then emit the
        # top `size` that pass filters. Ranking needs every candidate, so the
        # WHOLE window is consumed (window-based pagination) — see the spec.
        # Anchor the re-rank on the raw `query`, not the organism-expanded
        # `effective_query`: the boolean-expanded string ("(q) AND (syn1 OR syn2)")
        # is a poor embedding anchor, and `merged` is already organism-filtered by
        # the fan-out, so query-relevance within that set is the right signal.
        reordered, reason = await embeddings.rerank(client, query, merged)
        if reason:
            errors["semantic"] = reason
        merged = reordered
        emitted = []
        for r in merged:
            if _passes_filters(r, filters):
                emitted.append(r)
                if len(emitted) == size:
                    break
        consumed = merged
        cut = len(merged) - 1
    else:
        emitted = []
        cut = -1
        for i, r in enumerate(merged):
            cut = i
            if _passes_filters(r, filters):
                emitted.append(r)
                if len(emitted) == size:
                    break
        if cut < 0:
            cut = len(merged) - 1
        consumed = merged[: cut + 1]

    consumed_per_adapter = Counter(origin[id(r)] for r in consumed)
    new_offsets = {n: offsets.get(n, 0) + consumed_per_adapter.get(n, 0) for n in names}

    # More results remain if we left fetched candidates unconsumed, OR any source
    # still has rows past our advanced offset. Using the upstream total (not
    # len(recs)==size) is robust to the page-boundary slice that makes a paged
    # adapter return < size records even when it has more.
    #
    # `bool(merged)` guard: an empty page consumed nothing, so offsets could not
    # advance — emitting a cursor here would replay the identical window forever
    # (e.g. an adapter that reports total>0 but returns []). No candidates fetched
    # ⇒ no way to page forward ⇒ stop.
    more = bool(merged) and (
        (cut < len(merged) - 1) or any(new_offsets.get(n, 0) < totals.get(n, 0) for n in names)
    )
    next_cursor = (
        _cursor.encode(
            {
                "q": query,
                "sources": sources,
                "organism": organism,
                "disease": disease,
                "tissue": tissue,
                "chemical": chemical,
                "assay": assay,
                "filters": filters,
                "size": size,
                "offsets": new_offsets,
                "rank": rank,
                "collapse_mirrors": collapse_mirrors,
            }
        )
        if more
        else None
    )

    enriched = await _enrich(client, emitted, errors)
    enriched = [_with_version_status(r) for r in enriched]
    # Presentation-layer fold ONLY: collapse runs after offset/cursor accounting
    # (computed from `consumed`/`new_offsets` above) so it can never corrupt
    # pagination — a folded mirror just makes this page return fewer than `size`.
    if collapse_mirrors:
        enriched = _collapse_mirrors(enriched)
    return SearchResult(
        query=query,
        total=total,
        count=len(enriched),
        results=enriched,
        errors=errors,
        next_cursor=next_cursor,
        taxon_expansion=expansion,
        mesh_expansion=disease_expansion,
        tissue_expansion=tissue_expansion,
        chemical_expansion=chemical_expansion,
        assay_expansion=assay_expansion,
    )


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = 10,
    sources: list[str] | None = None,
    organism: str | None = None,
) -> tuple[int, list[DataResource], dict[str, str], TaxonExpansion | None]:
    """Legacy 4-tuple entrypoint, preserved for existing callers/tests. Delegates
    to :func:`search_page` (page 1, no filters) and unpacks its model.
    Returns ``(total, deduped_results, errors, taxon_expansion)``.
    """
    r = await search_page(client, query=query, size=size, sources=sources, organism=organism)
    return r.total, r.results, r.errors, r.taxon_expansion


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Route ``resolve`` by id shape, then enrich with normalized taxa + links.
    - ``geo:``/``sra:``/``bioproject:``  → omics (NCBI)
    - ``pubmed:``/``openaire:``          → literature
    - ``dataone:<pid>``                  → DataONE (verified fetch)
    - ``omicsdi:<source>:<acc>``         → OmicsDI (routes fetch to PRIDE/MetaboLights)
    - ``datacite:<doi>``                 → DataCite
    - ``zenodo:<id>`` / bare digits      → Zenodo (native; carries files[])
    - ``hf:<owner>/<name>``              → HuggingFace (native; carries files[])
    - a bare DOI (contains ``/``)        → DataCite
    """
    rid = resource_id.strip()
    cached = _RESOLVE_CACHE.get(rid)
    if cached is not MISS:
        return cached
    prefix = rid.split(":", 1)[0]
    if prefix in omics.PREFIXES:
        resource = await omics.resolve(client, rid)
    elif prefix in literature.PREFIXES:
        resource = await literature.resolve(client, rid)
    elif prefix in dataone.PREFIXES:
        resource = await dataone.resolve(client, rid)
    elif prefix in dandi.PREFIXES:
        resource = await dandi.resolve(client, rid)
    elif prefix in cellxgene.PREFIXES:
        resource = await cellxgene.resolve(client, rid)
    elif prefix in omicsdi.PREFIXES:
        resource = await omicsdi.resolve(client, rid)
    elif prefix in openml.PREFIXES:
        resource = await openml.resolve(client, rid)
    elif prefix in pdb.PREFIXES:
        resource = await pdb.resolve(client, rid)
    elif prefix in gwas.PREFIXES:
        resource = await gwas.resolve(client, rid)
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
            "'geo:/sra:/bioproject:<acc>', 'pubmed:/openaire:<id>', 'dataone:<pid>', "
            "'omicsdi:<source>:<acc>', 'dandi:<id>', 'cellxgene:<id>', 'openml:<id>', "
            "'pdb:<id>', 'gwas:<acc>', "
            "a bare Zenodo id, or a DOI"
        )
    if resource.organism:
        try:
            resource = await _enrich_resource(client, resource)
        except Exception as exc:  # additive enrichment must not sink a valid resolve
            logger.warning("resolve enrichment failed for %s: %r", rid, exc)
    is_latest, superseded_by = derive_version_status(resource.links)
    if is_latest is not None or superseded_by is not None:
        resource = resource.model_copy(
            update={"is_latest": is_latest, "superseded_by": superseded_by}
        )
    resource = resource.model_copy(
        update={
            "access_modes": derive_access_modes(resource.files, operate=operate.OPERATE_AVAILABLE)
        }
    )
    _RESOLVE_CACHE.set(rid, resource)
    return resource
