"""Multi-source router for the archives layer.

Fans out adapter ``search`` coroutines in parallel, normalizes into one
``DataResource`` stream, dedups by DOI (native fetch backends win over
DataCite metadata), and routes ``resolve`` by id prefix. Per-source failures
are captured into an errors map and surfaced — never silently swallowed (a
dropped adapter would make the model conclude "no data exists").

This module is the ORCHESTRATOR. Two self-contained policies it only sequences
live next door, and the dependency runs one way — neither imports back:

* ``_mirror``   — record identity: exact-DOI dedup + cross-repo mirror collapse.
* ``_ontology`` — entity → synonym-expanded query, plus the unresolved echo.

Both are re-exported below under their historical private names, so the merge
path's existing callers and tests address live code rather than a copy.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from typing import Any

import httpx

from data_aggregator_mcp import (
    _cursor,
    _mirror,
    _ontology,
    datacite,
    embeddings,
    operate,
    sources,
    taxonomy,
    zenodo,
)
from data_aggregator_mcp import query_understanding as query_understanding_mod
from data_aggregator_mcp import relate as relate_mod
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import (
    AssayExpansion,
    ChemicalExpansion,
    DataResource,
    Link,
    MeshExpansion,
    QueryExpansion,
    QueryUnderstanding,
    RelateResult,
    SearchResult,
    Taxon,
    TaxonExpansion,
    TissueExpansion,
    UnresolvedEntity,
    derive_access_modes,
    derive_version_status,
)

_VALID_KINDS = {"dataset", "sequencing_run", "study", "publication", "software"}

# A2.P2: hard cap on the number of query variants fanned out (incl. the original as
# variant 0). The upstream fan-out is N variants × M sources × size, so this bounds the
# N× cost. The original query is ALWAYS variant 0, so recall never drops below baseline.
MAX_QUERY_VARIANTS = 4

logger = logging.getLogger(__name__)


def _dedup_ci(queries: list[str]) -> list[str]:
    """Case-insensitively dedup a list of query strings, preserving first-seen order.
    Used to assemble the multi-query variant list (the original is always first, so it
    survives dedup and stays variant 0)."""
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


# Name → adapter module, in dedup-precedence order. Single source of truth is the central
# registry (sources.py); it also derives the fetch gate and the discovery-only set, so the
# resolve dispatch below can never drift out of sync with them (that gap was the uniprot bug).
_ADAPTERS: dict[str, Any] = sources.ADAPTERS


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


# Cross-source record identity (exact-DOI dedup + mirror collapse) lives in _mirror;
# ontology query expansion lives in _ontology. Both are pure policy this module only
# sequences. Re-exported under their historical private names: they are the internal
# surface the merge path — and its tests — already address.
_DISCOVERY_ONLY_SOURCES = _mirror.DISCOVERY_ONLY_SOURCES
_fetch_priority = _mirror.fetch_priority
_dedup = _mirror.dedup_by_doi
_normalize_title = _mirror.normalize_title
_first_author_surname = _mirror.first_author_surname
_fingerprint_key = _mirror.fingerprint_key
_checksums = _mirror.checksums
_survivor_rank = _mirror.survivor_rank
_collapse_mirrors = _mirror.collapse_mirrors

_or_group = _ontology.or_group
_ONTOLOGY_FIELDS = _ontology.ONTOLOGY_FIELDS
_unresolved_entities = _ontology.unresolved_entities
_expand_organism = _ontology.expand_organism
_expand_disease = _ontology.expand_disease
_expand_tissue = _ontology.expand_tissue
_expand_chemical = _ontology.expand_chemical
_expand_assay = _ontology.expand_assay


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
    """Enrich each organism-bearing resource with taxon info, concurrently. A taxonomy
    failure is recorded once in ``errors['taxonomy']`` and that resource is returned
    un-enriched; the others still enrich.

    Concurrent (was sequential): the RTT-serialized awaits left an ``NCBI_API_KEY``'s
    10/s budget unused. The shared token-bucket limiter and ``resolve_taxon``'s cache
    bound the fan-out (page size <= 50), so a gather is safe and ~2-3x faster on a cold
    page. A down NCBI now costs one bounded burst per page instead of a single probe —
    an acceptable trade for the common-case latency win.
    """
    results = await asyncio.gather(
        *(_enrich_resource(client, r) if r.organism else _identity(r) for r in resources),
        return_exceptions=True,
    )
    out: list[DataResource] = []
    for original, res in zip(resources, results, strict=True):
        if isinstance(res, BaseException):
            errors.setdefault("taxonomy", f"{type(res).__name__}: {res}")
            out.append(original)
        else:
            out.append(res)
    return out


async def _identity(r: DataResource) -> DataResource:
    """Await-able passthrough so gather() can mix enriched and organism-less resources."""
    return r


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


def _comp_key(vi: int, name: str) -> str:
    """Serialize a composite ``(variant_index, source)`` offset key for the multi-query
    cursor. JSON object keys must be strings, so we join with a separator that cannot
    appear in a variant index (digits) — the source name follows the first colon."""
    return f"{vi}:{name}"


def _comp_unkey(key: str) -> tuple[int, str]:
    """Inverse of :func:`_comp_key`. The variant index is the prefix before the first
    colon; the source name (which may itself contain no colon for our adapters) follows."""
    vi_str, name = key.split(":", 1)
    return int(vi_str), name


async def _build_search_result(
    client: httpx.AsyncClient,
    *,
    query: str,
    total: int,
    emitted: list[DataResource],
    errors: dict[str, str],
    next_cursor: str | None,
    collapse_mirrors: bool,
    taxon_expansion: TaxonExpansion | None = None,
    mesh_expansion: MeshExpansion | None = None,
    tissue_expansion: TissueExpansion | None = None,
    chemical_expansion: ChemicalExpansion | None = None,
    assay_expansion: AssayExpansion | None = None,
    unresolved: list[UnresolvedEntity] | None = None,
    query_understanding: QueryUnderstanding | None = None,
    query_expansion: QueryExpansion | None = None,
) -> SearchResult:
    """Shared TAIL for both the single-query and multi-query paths: enrich → version
    status → optional mirror-collapse → assemble the ``SearchResult``. Offset/cursor
    accounting is done by the caller (so collapse can never corrupt pagination)."""
    enriched = await _enrich(client, emitted, errors)
    enriched = [_with_version_status(r) for r in enriched]
    # Presentation-layer fold ONLY: collapse runs after offset/cursor accounting so it
    # can never corrupt pagination — a folded mirror just makes this page return fewer
    # than `size`.
    if collapse_mirrors:
        enriched = _collapse_mirrors(enriched)
    return SearchResult(
        query=query,
        total=total,
        count=len(enriched),
        results=enriched,
        errors=errors,
        next_cursor=next_cursor,
        taxon_expansion=taxon_expansion,
        mesh_expansion=mesh_expansion,
        tissue_expansion=tissue_expansion,
        chemical_expansion=chemical_expansion,
        assay_expansion=assay_expansion,
        unresolved=list(unresolved or []),
        query_understanding=query_understanding,
        query_expansion=query_expansion,
    )


async def _multi_query_page(
    client: httpx.AsyncClient,
    *,
    original_query: str,
    variants: list[str],
    size: int,
    sources: list[str] | None,
    filters: dict[str, Any],
    comp_offsets: dict[str, int],
    collapse_mirrors: bool,
    errors: dict[str, str],
    query_expansion: QueryExpansion | None,
    taxon_expansion: TaxonExpansion | None = None,
    mesh_expansion: MeshExpansion | None = None,
    tissue_expansion: TissueExpansion | None = None,
    chemical_expansion: ChemicalExpansion | None = None,
    assay_expansion: AssayExpansion | None = None,
    unresolved: list[UnresolvedEntity] | None = None,
    query_understanding: QueryUnderstanding | None = None,
) -> SearchResult:
    """A2.P2 parallel multi-query fan-out keyed by a composite ``(variant_index, source)``
    label. ``variants`` are the ALREADY-EXPANDED effective query strings (variant 0 is the
    post-understand/post-expansion original). Fans every variant × source at its composite
    offset, merges + dedups the union (cross-variant duplicates collapse to one), and
    re-ranks the WHOLE window against ``original_query`` before emitting the top ``size``.
    Pagination advances per composite key and the cursor stores the expanded variants, so a
    continuation re-fans the frozen variants with NO LLM / NO re-expand."""
    adapters = _select(sources)
    names = list(adapters)
    keys = [(vi, name) for vi in range(len(variants)) for name in names]
    outcomes = await asyncio.gather(
        *(
            adapters[name].search(
                client, variants[vi], size=size, offset=comp_offsets.get(_comp_key(vi, name), 0)
            )
            for (vi, name) in keys
        ),
        return_exceptions=True,
    )

    origin: dict[int, str] = {}  # id(record) -> composite key string
    per_stream: list[list[DataResource]] = []
    comp_totals: dict[str, int] = {}
    total = 0
    for (vi, name), outcome in zip(keys, outcomes, strict=False):
        ckey = _comp_key(vi, name)
        if isinstance(outcome, BaseException):
            # Surface a per-variant×source failure (including asyncio.CancelledError)
            # without clobbering another variant's error for the same source.
            errors[f"{name}#v{vi}"] = f"{type(outcome).__name__}: {outcome}"
            comp_totals[ckey] = 0
            continue
        assert isinstance(outcome, tuple)
        adapter_total, recs = outcome
        total += adapter_total
        comp_totals[ckey] = adapter_total
        for r in recs:
            origin[id(r)] = ckey
        per_stream.append(recs)

    merged = _dedup(interleave(per_stream))

    # Window-rank ALWAYS for multi-query: the union has no single coherent upstream order,
    # so re-rank the whole window against the ORIGINAL pre-expansion query and consume all
    # of it. No embedding endpoint → interleaved order + errors["semantic"] (still a recall
    # win, just unranked).
    reordered, reason = await embeddings.rerank(client, original_query, merged)
    if reason:
        errors["semantic"] = reason
    merged = reordered
    emitted: list[DataResource] = []
    for r in merged:
        if _passes_filters(r, filters):
            emitted.append(r)
            if len(emitted) == size:
                break
    # Multi-query always window-consumes the full merged+reranked window (no partial
    # consume), so every fetched record advances its stream offset. More results
    # remain iff any stream still has rows past the new offset.
    consumed = merged

    consumed_per_stream: Counter[str] = Counter(origin[id(r)] for r in consumed)
    new_comp_offsets = {
        _comp_key(vi, name): comp_offsets.get(_comp_key(vi, name), 0)
        + consumed_per_stream.get(_comp_key(vi, name), 0)
        for (vi, name) in keys
    }

    # `bool(merged)` guard mirrors the single-query path: an empty window consumed nothing,
    # so a replayed cursor would loop forever.
    more = bool(merged) and any(
        new_comp_offsets.get(_comp_key(vi, name), 0) < comp_totals.get(_comp_key(vi, name), 0)
        for (vi, name) in keys
    )
    next_cursor = (
        _cursor.encode(
            {
                "q": original_query,
                "sources": sources,
                "variants": variants,
                "filters": filters,
                "size": size,
                "offsets": new_comp_offsets,
                "collapse_mirrors": collapse_mirrors,
            }
        )
        if more
        else None
    )

    return await _build_search_result(
        client,
        query=original_query,
        total=total,
        emitted=emitted,
        errors=errors,
        next_cursor=next_cursor,
        collapse_mirrors=collapse_mirrors,
        taxon_expansion=taxon_expansion,
        mesh_expansion=mesh_expansion,
        tissue_expansion=tissue_expansion,
        chemical_expansion=chemical_expansion,
        assay_expansion=assay_expansion,
        unresolved=unresolved,
        query_understanding=query_understanding,
        query_expansion=query_expansion,
    )


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
    understand: bool = False,
    multi_query: bool = False,
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
        # Multi-query cursor (A2.P2): identified by a `variants` key. The stored variants
        # are already-EXPANDED effective query strings, so the continuation re-fans them
        # with NO LLM and NO re-expand. Single-query cursors (no `variants`) fall through
        # to the byte-identical path below.
        if "variants" in st:
            return await _multi_query_page(
                client,
                original_query=st["q"],
                variants=st["variants"],
                size=st["size"],
                sources=st.get("sources"),
                filters=st.get("filters") or {},
                comp_offsets=st["offsets"],
                collapse_mirrors=st.get("collapse_mirrors", False),
                errors={},
                query_expansion=None,  # echo is page-1 only; frozen None on continuation
            )
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
        # Frozen to [] like every other echo — and it MUST NOT be derived here: the
        # ontology params above are restored from the cursor while the expansions are
        # deliberately frozen None, so `_unresolved_entities` would read that as "every
        # supplied param matched nothing" and cry wolf on every page after the first.
        unresolved: list[UnresolvedEntity] = []
        query_understanding = None  # frozen on continuation; never re-understand
        effective_query = query
        errors: dict[str, str] = {}
    else:
        if query is None:
            raise ValidationError("search requires either 'query' or 'cursor'")
        errors = {}
        query_understanding = None
        # Capture the ORIGINAL user query BEFORE understand/expansion mutate `query`. This
        # is the multi-query re-rank anchor and the `query_expansion.input` echo.
        original_query = query
        if understand:
            raw_query = query  # echo the original query, captured before any rewrite
            ru = await query_understanding_mod.rewrite(client, query)
            if ru is None:
                errors["understand"] = (
                    "query understanding unavailable (no LLM endpoint configured or rewrite failed)"
                )
            else:
                # understand=true NORMALIZES the query; it does NOT auto-impose structured
                # filters. keyword_core (entity-rich, fluff-stripped) replaces the query, and
                # explicit year scopes are applied. The entity facets (organism/disease/tissue/
                # chemical/assay) and kind are ECHOED in `extracted` for transparency but NEVER
                # auto-applied: ANDing LLM-INFERRED facets across free-text keyword upstreams
                # over-constrains and tanks recall (measured mean recall@20 lift -0.40 when they
                # were auto-applied; root cause = the _expand_* AND-clauses + the kind post-filter
                # dropping records the metadata never satisfies — see scripts/eval_understand.py).
                # Only CALLER-passed facets drive the _expand_* resolvers / kind filter below.
                extracted: dict[str, Any] = {}
                applied: dict[str, Any] = {}
                overridden: list[str] = []
                keyword_core = ru.keyword_core
                if keyword_core:
                    extracted["keyword_core"] = keyword_core
                    applied["keyword_core"] = keyword_core
                    query = keyword_core
                # Entity facets + kind: ADVISORY echo only (never auto-applied — see above).
                for _name, _val in (
                    ("organism", ru.organism),
                    ("disease", ru.disease),
                    ("tissue", ru.tissue),
                    ("chemical", ru.chemical),
                    ("assay", ru.assay),
                    ("kind", ru.kind),
                ):
                    if _val is not None:
                        extracted[_name] = _val
                # Year scopes: safe scalar bounds reflecting EXPLICIT temporal intent — applied
                # (caller value wins).
                if ru.year_min is not None:
                    extracted["year_min"] = ru.year_min
                    if published_after is not None:
                        overridden.append("year_min")
                    else:
                        published_after = ru.year_min
                        applied["year_min"] = ru.year_min
                if ru.year_max is not None:
                    extracted["year_max"] = ru.year_max
                    if published_before is not None:
                        overridden.append("year_max")
                    else:
                        published_before = ru.year_max
                        applied["year_max"] = ru.year_max
                query_understanding = QueryUnderstanding(
                    input=raw_query,
                    keyword_core=keyword_core,
                    extracted=extracted,
                    applied=applied,
                    overridden=overridden,
                    confidence=ru.confidence,
                )
        filters = {
            "published_after": published_after,
            "published_before": published_before,
            "kind": kind,
        }
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
        # Computed once, here, where all five echoes are in scope — NOT inside the
        # variant loop below, which re-runs the same (cached) expansions and drops
        # their echoes on purpose.
        unresolved = _unresolved_entities(
            {
                "organism": organism,
                "disease": disease,
                "tissue": tissue,
                "chemical": chemical,
                "assay": assay,
            },
            {
                "organism": expansion,
                "disease": disease_expansion,
                "tissue": tissue_expansion,
                "chemical": chemical_expansion,
                "assay": assay_expansion,
            },
            errors,
        )

        if multi_query:
            # A2.P2 parallel path. Variant 0 = the post-understand/post-expansion
            # `effective_query` (so recall never drops below the single-query baseline).
            # Ask the LLM for diverse reformulations; on failure, fall through to the
            # byte-identical single-query path below with a transparency note.
            variants_raw = await query_understanding_mod.expand(client, query, n=MAX_QUERY_VARIANTS)
            if variants_raw is None:
                errors["multi_query"] = (
                    "multi-query expansion unavailable "
                    "(no LLM endpoint configured or expansion failed)"
                )
            else:
                # Raw variant list for the echo: original (post-understand) query first,
                # ci-deduped, capped. Variant 0 is always the original.
                raw_variants = _dedup_ci([query, *variants_raw])[:MAX_QUERY_VARIANTS]
                # Effective (ontology-expanded) string per variant. Variant 0 reuses the
                # already-computed `effective_query`; the rest run the SAME expansion chain
                # (resolver lookups are cached → cheap). Echoes were captured once above.
                eff_variants = [effective_query]
                for raw in raw_variants[1:]:
                    eff, _ = await _expand_organism(client, raw, organism, errors)
                    eff, _ = await _expand_disease(client, eff, disease, errors)
                    eff, _ = await _expand_tissue(client, eff, tissue, errors)
                    eff, _ = await _expand_chemical(client, eff, chemical, errors)
                    eff, _ = await _expand_assay(client, eff, assay, errors)
                    eff_variants.append(eff)
                return await _multi_query_page(
                    client,
                    original_query=original_query,
                    variants=eff_variants,
                    size=size,
                    sources=sources,
                    filters={
                        "published_after": published_after,
                        "published_before": published_before,
                        "kind": kind,
                    },
                    comp_offsets={},
                    collapse_mirrors=collapse_mirrors,
                    errors=errors,
                    query_expansion=QueryExpansion(input=original_query, variants=raw_variants),
                    taxon_expansion=expansion,
                    mesh_expansion=disease_expansion,
                    tissue_expansion=tissue_expansion,
                    chemical_expansion=chemical_expansion,
                    assay_expansion=assay_expansion,
                    unresolved=unresolved,
                    query_understanding=query_understanding,
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
        if isinstance(outcome, BaseException):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            totals[name] = 0
            continue
        # gather(return_exceptions=True) delivers either a BaseException instance or
        # the success value; the BaseException guard above handles all error cases
        # (including asyncio.CancelledError which is not an Exception since Python 3.8).
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
        unresolved=unresolved,
        query_understanding=query_understanding,
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
    - ``gbif:<dataset-key>``             → GBIF (unverified DwC-A fetch)
    - ``datagov:<name-slug>``            → data.gov (CKAN; unverified resource fetch)
    - ``nasacmr:<concept-id>``           → NASA CMR (Earthdata; discovery-only)
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
    module = sources.resolver_for(prefix)
    if module is not None:
        resource = await module.resolve(client, rid)
    elif rid.isdigit():  # a bare Zenodo record id
        resource = await zenodo.resolve(client, rid)
    elif "/" in rid:  # a bare DOI
        resource = await datacite.resolve(client, rid)
    else:
        raise ValueError(
            f"cannot route id {resource_id!r}: expected 'zenodo:<id>', 'datacite:<doi>', "
            "'geo:/sra:/bioproject:<acc>', 'pubmed:/openaire:<id>', 'dataone:<pid>', "
            "'gbif:<dataset-key>', 'datagov:<name-slug>', 'nasacmr:<concept-id>', "
            "'omicsdi:<source>:<acc>', 'dandi:<id>', "
            "'cellxgene:<id>', 'openml:<id>', "
            "'pdb:<id>', 'uniprot:<acc>', 'gwas:<acc>', 'biostudies:<acc>', "
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


RELATE_MAX_IDS = 10


async def relate(client: httpx.AsyncClient, ids: list[str]) -> RelateResult:
    """Resolve `ids` (TTL-cached, concurrent, fail-soft) and return metadata-level
    join/harmonization hints. 2..RELATE_MAX_IDS ids; <2 or >max -> ValidationError."""
    if not isinstance(ids, list) or len(ids) < 2:
        raise ValidationError("relate needs at least 2 ids")
    if len(ids) > RELATE_MAX_IDS:
        raise ValidationError(f"relate accepts at most {RELATE_MAX_IDS} ids; got {len(ids)}")

    settled = await asyncio.gather(*(resolve(client, i) for i in ids), return_exceptions=True)
    resolved: list[DataResource] = []
    resolved_ids: list[str] = []
    errors: dict[str, str] = {}
    for given, res in zip(ids, settled, strict=True):
        if isinstance(res, BaseException):
            errors[given] = f"{type(res).__name__}: {res}"
        else:
            resolved.append(res)
            resolved_ids.append(res.id)

    hints = relate_mod.detect(resolved) if len(resolved) >= 2 else []
    note: str | None = None
    if len(resolved) < 2:
        note = f"fewer than 2 ids resolved ({len(resolved)}); need 2+ to compare"
    elif not hints:
        note = f"no structural relationships detected among {len(resolved)} resources"
    return RelateResult(input_ids=ids, resolved=resolved_ids, hints=hints, errors=errors, note=note)
