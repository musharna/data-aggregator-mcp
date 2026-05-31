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
from collections import Counter
from typing import Any

import httpx

from data_aggregator_mcp import _cursor, datacite, literature, omics, taxonomy, zenodo
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.errors import ValidationError
from data_aggregator_mcp.models import DataResource, Link, SearchResult, Taxon, TaxonExpansion

_VALID_KINDS = {"dataset", "sequencing_run", "study", "publication", "software"}

logger = logging.getLogger(__name__)

# Registration order = merge precedence: native fetch backends first so that on
# a DOI collision the fetchable record is encountered before the DataCite one.
_ADAPTERS: dict[str, Any] = {
    "zenodo": zenodo,
    "datacite": datacite,
    "omics": omics,
    "literature": literature,
}


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
    or_group = " OR ".join(f'"{t}"' for t in terms)
    effective = f"({query}) AND ({or_group})"
    expansion = TaxonExpansion(
        input=organism,
        taxid=info.taxid,
        canonical_name=info.canonical_name,
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
    if pb is not None and r.year > pb:
        return False
    return True


async def search_page(
    client: httpx.AsyncClient,
    *,
    query: str | None = None,
    size: int = 10,
    sources: list[str] | None = None,
    organism: str | None = None,
    published_after: int | None = None,
    published_before: int | None = None,
    kind: str | None = None,
    cursor: str | None = None,
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
        expansion = None  # frozen on continuation; do not re-expand
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
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, Exception):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            totals[name] = 0
            continue
        adapter_total, recs = outcome
        total += adapter_total
        totals[name] = adapter_total
        for r in recs:
            origin[id(r)] = name
        per_source.append(recs)

    merged = _dedup(interleave(per_source))

    emitted: list[DataResource] = []
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
    more = (cut < len(merged) - 1) or any(new_offsets.get(n, 0) < totals.get(n, 0) for n in names)
    next_cursor = (
        _cursor.encode(
            {
                "q": query,
                "sources": sources,
                "organism": organism,
                "filters": filters,
                "size": size,
                "offsets": new_offsets,
            }
        )
        if more
        else None
    )

    enriched = await _enrich(client, emitted, errors)
    return SearchResult(
        query=query,
        total=total,
        count=len(enriched),
        results=enriched,
        errors=errors,
        next_cursor=next_cursor,
        taxon_expansion=expansion,
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
    - ``datacite:<doi>``                 → DataCite
    - ``zenodo:<id>`` / bare digits      → Zenodo (native; carries files[])
    - a bare DOI (contains ``/``)        → DataCite
    """
    rid = resource_id.strip()
    prefix = rid.split(":", 1)[0]
    if prefix in omics.PREFIXES:
        resource = await omics.resolve(client, rid)
    elif prefix in literature.PREFIXES:
        resource = await literature.resolve(client, rid)
    elif rid.startswith("datacite:"):
        resource = await datacite.resolve(client, rid)
    elif rid.startswith("zenodo:") or rid.isdigit():
        resource = await zenodo.resolve(client, rid)
    elif "/" in rid:
        resource = await datacite.resolve(client, rid)
    else:
        raise ValueError(
            f"cannot route id {resource_id!r}: expected 'zenodo:<id>', 'datacite:<doi>', "
            "'geo:/sra:/bioproject:<acc>', 'pubmed:/openaire:<id>', a bare Zenodo id, or a DOI"
        )
    if resource.organism:
        try:
            resource = await _enrich_resource(client, resource)
        except Exception as exc:  # additive enrichment must not sink a valid resolve
            logger.warning("resolve enrichment failed for %s: %r", rid, exc)
    return resource
