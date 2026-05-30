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
from typing import Any

import httpx

from data_aggregator_mcp import datacite, literature, omics, taxonomy, zenodo
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.models import DataResource, Link, Taxon, TaxonExpansion

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


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = 10,
    sources: list[str] | None = None,
    organism: str | None = None,
) -> tuple[int, list[DataResource], dict[str, str], TaxonExpansion | None]:
    """Fan out ``query`` to selected adapters concurrently, merge + dedup.

    When ``organism`` is given it is resolved against NCBI Taxonomy and the
    query is expanded with the canonical name + synonyms (synonym expansion).
    Returns ``(total, deduped_results, errors, taxon_expansion)``.
    """
    adapters = _select(sources)
    names = list(adapters)
    errors: dict[str, str] = {}
    effective_query, expansion = await _expand_organism(client, query, organism, errors)
    outcomes = await asyncio.gather(
        *(adapters[n].search(client, effective_query, size=size) for n in names),
        return_exceptions=True,
    )
    per_source: list[list[DataResource]] = []
    total = 0
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, Exception):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            continue
        adapter_total, recs = outcome
        total += adapter_total
        per_source.append(recs)
    merged = _dedup(interleave(per_source))[:size]
    enriched = await _enrich(client, merged, errors)
    return total, enriched, errors, expansion


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
