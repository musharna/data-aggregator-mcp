"""Central source registry — one row per wired source: its adapter module, its routing
prefixes, its fetchability, and the human-facing catalog metadata ``list_sources`` returns.
``router._ADAPTERS`` / ``router._DISCOVERY_ONLY_SOURCES``, ``server._FETCHABLE_SOURCES`` and
``server._SOURCES`` all derive from it.

This module depends on the adapters but on neither ``router`` nor ``server``, so both can
import it without a cycle. Before this, per-source routing/fetchability was restated in
four places kept in lockstep only by tests; a miss produced the ``uniprot`` bug (registered
+ fetchable, but absent from the resolve dispatch chain — resolve/fetch unreachable). Adding
a source is now one row here.

``fetchable`` is a single declaration doing two jobs: it gates fetch AND is the label
``list_sources`` advertises. ``False`` = discovery-only; ``True`` = every prefix fetchable;
a string ("per-repo", "per-dataset", …) = fetchable but decided per record, shown verbatim.
So the gate and the advertised label cannot disagree — they used to be separate literals in
separate modules, checked only by a test.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from data_aggregator_mcp import (
    biostudies,
    cellxgene,
    dandi,
    datacite,
    datagov,
    dataone,
    gbif,
    gwas,
    huggingface,
    literature,
    nasacmr,
    omics,
    omicsdi,
    openml,
    pdb,
    uniprot,
    zenodo,
)
from data_aggregator_mcp.models import DataResource


@runtime_checkable
class SourceAdapter(Protocol):
    """The contract every adapter module in ``SOURCES`` satisfies.

    Adapters are modules, not classes — this Protocol is what makes that structural
    contract checkable instead of implied. It was previously spelled ``Any``, so a
    registered module missing ``resolve`` (or carrying a drifted signature) type-checked
    fine and only failed at runtime on the id that happened to route to it — the same
    class of latent gap as the ``uniprot`` bug.

    ``client`` and the id are positional-only here because adapters disagree on the
    second parameter's name (``record_id`` in zenodo, ``resource_id`` elsewhere) and the
    router only ever passes them positionally. ``PREFIXES`` is a read-only property so
    the concrete types can vary (``frozenset`` / ``set`` / ``tuple``), which they do.
    """

    @property
    def PREFIXES(self) -> Collection[str]: ...

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        /,
        *,
        size: int = ...,
        offset: int = ...,
    ) -> tuple[int, list[DataResource]]:
        """``(total_hits, page)``. ``total`` is the source's own reported total, which
        may be an estimate; ``page`` holds at most ``size`` records."""
        ...

    async def resolve(self, client: httpx.AsyncClient, resource_id: str, /) -> DataResource:
        """Full record for one id. Raises ``NotFoundError`` if the source has no such id."""
        ...


@dataclass(frozen=True)
class SourceSpec:
    """One wired source. ``prefixes`` are every id prefix it resolves; ``fetchable_prefixes``
    is the subset with a working fetch backend (usually all of them — but e.g. omics routes
    ``bioproject`` for discovery yet only ``geo``/``sra`` are fetchable). A source is
    discovery-only (no fetch, lowest DOI-dedup precedence) exactly when
    ``fetchable_prefixes`` is empty."""

    name: str
    module: SourceAdapter
    prefixes: frozenset[str]
    fetchable_prefixes: frozenset[str]
    # Human-facing catalog metadata — the list_sources tool payload.
    layer: str
    kinds: tuple[str, ...]
    filters_supported: tuple[str, ...]
    rate_limit: str
    status: str
    id_example: str
    fetchable: bool | str
    auth_required: bool = False
    operable: bool | None = None
    fetchable_notes: str | None = None
    description: str | None = None

    def catalog_entry(self) -> dict[str, Any]:
        """This source's ``list_sources`` row. Unset optional keys stay ABSENT rather than
        None, and key order is fixed: the payload is a public tool contract."""
        entry: dict[str, Any] = {
            "name": self.name,
            "layer": self.layer,
            "kinds": list(self.kinds),
            "filters_supported": list(self.filters_supported),
            "auth_required": self.auth_required,
            "rate_limit": self.rate_limit,
            "status": self.status,
            "fetchable": self.fetchable,
        }
        if self.operable is not None:
            entry["operable"] = self.operable
        if self.fetchable_notes is not None:
            entry["fetchable_notes"] = self.fetchable_notes
        entry["id_example"] = self.id_example
        if self.description is not None:
            entry["description"] = self.description
        return entry


def _spec(
    name: str,
    module: SourceAdapter,
    *,
    layer: str,
    kinds: tuple[str, ...],
    filters_supported: tuple[str, ...],
    rate_limit: str,
    status: str,
    id_example: str,
    fetchable: bool | str = True,
    fetchable_prefixes: Collection[str] | None = None,
    auth_required: bool = False,
    operable: bool | None = None,
    fetchable_notes: str | None = None,
    description: str | None = None,
) -> SourceSpec:
    prefixes = frozenset(module.PREFIXES)
    if fetchable is False:
        if fetchable_prefixes is not None:  # contradictory input — don't silently pick one
            raise ValueError(
                f"source {name!r} is declared fetchable=False but also names "
                f"fetchable_prefixes={sorted(fetchable_prefixes)!r}"
            )
        fp: frozenset[str] = frozenset()
    elif fetchable_prefixes is None:
        fp = prefixes
    else:
        fp = frozenset(fetchable_prefixes)
    # Fail loud rather than advertise a fetchability the router won't honour — that
    # mismatch IS the uniprot bug class, just in the other direction.
    if bool(fp) != bool(fetchable):
        raise ValueError(
            f"source {name!r} advertises fetchable={fetchable!r} but its fetchable prefixes "
            f"are {sorted(fp)!r} — the advertised label and the fetch gate must agree"
        )
    return SourceSpec(
        name=name,
        module=module,
        prefixes=prefixes,
        fetchable_prefixes=fp,
        layer=layer,
        kinds=kinds,
        filters_supported=filters_supported,
        rate_limit=rate_limit,
        status=status,
        id_example=id_example,
        fetchable=fetchable,
        auth_required=auth_required,
        operable=operable,
        fetchable_notes=fetchable_notes,
        description=description,
    )


# Order = _dedup merge precedence: fetchable natives before DataCite (so on a shared DOI the
# fetchable copy is seen first); discovery-only sources (gwas/nasacmr) registered late so a
# fetchable native still wins. See router._fetch_priority for the collision rule itself.
SOURCES: tuple[SourceSpec, ...] = (
    _spec(
        "zenodo",
        zenodo,
        layer="archives",
        kinds=("dataset", "publication", "software"),
        filters_supported=(
            "query",
            "size",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ),
        rate_limit="~60/min anonymous",
        status="live",
        fetchable=True,
        operable=True,
        id_example="zenodo:7654321",
    ),
    _spec(
        "dataone",
        dataone,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size", "cursor"),
        rate_limit="public CN; courtesy only",
        status="live (eco/environmental federation; verified fetch via Member Nodes)",
        fetchable=True,
        operable=True,
        fetchable_notes="Data objects fetched from Member Nodes with per-object MD5/SHA-256 verification.",
        id_example="dataone:doi:10.18739/A26336",
        description="DataONE federation of environmental & earth-science repositories (KNB, Arctic Data Center, PANGAEA, TERN, ...).",
    ),
    # GBIF DOIs share the 10.15468 DataCite prefix — the native must precede datacite.
    _spec(
        "gbif",
        gbif,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size", "cursor"),
        rate_limit="public API; courtesy only",
        status="live (biodiversity dataset registry; DOI-normalized, non-bio-omics)",
        fetchable="per-dataset",
        operable=False,
        fetchable_notes="Occurrence/checklist/sampling-event datasets fetch their Darwin Core Archive (unverified - no upstream checksum); metadata-only datasets are discovery-only.",
        id_example="gbif:6d27080f-ed47-48e2-90e8-cdebaba11a03",
        description="Global Biodiversity Information Facility - species-occurrence, checklist & sampling-event datasets with a DOI and a downloadable Darwin Core Archive.",
    ),
    _spec(
        "datagov",
        datagov,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size", "cursor"),
        auth_required=True,
        rate_limit="api.data.gov key; DEMO_KEY fallback ~30/hour per IP",
        status="live (US government open-data catalog; CKAN via the GSA Catalog API)",
        fetchable="per-dataset",
        operable=False,
        fetchable_notes="CKAN resources fetched by direct URL (unverified - no reliable upstream checksum); metadata-only packages are discovery-only. Needs an api.data.gov key via DATA_GOV_API_KEY (else the rate-limited DEMO_KEY).",
        id_example="datagov:civil-rights-data-collection-crdc",
        description="data.gov - the US government open-data catalog (climate, agriculture, economic, civic & scientific datasets); non-biological breadth beyond the omics core.",
    ),
    _spec(
        "cellxgene",
        cellxgene,
        layer="omics",
        kinds=("dataset",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (CZ CELLxGENE Discover collections search/resolve; asset manifest on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="H5AD/RDS assets stream from datasets.cellxgene.cziscience.com (direct URLs, unverified — no checksum in the API); the per-collection manifest is capped at 200 files for large atlases.",
        id_example="cellxgene:af893e86-8e9f-41f1-a474-ef05359b1fb7",
        description="CZ CELLxGENE Discover — single-cell datasets grouped by collection (one publication DOI per collection); search filters on tissue/disease/organism/assay, resolve attaches the H5AD/RDS download manifest.",
    ),
    _spec(
        "datacite",
        datacite,
        layer="archives",
        kinds=("dataset", "publication", "software"),
        filters_supported=("query", "published_after", "published_before", "kind", "cursor"),
        rate_limit="respects 429/Retry-After",
        status="live (discovery; fetch on resolve for Figshare/Dataverse/OSF/Zenodo, manifest-only for Dryad)",
        fetchable="per-repo",
        operable=True,
        fetchable_notes="Figshare/Dataverse/OSF/Zenodo fetchable; OpenNeuro (10.18112/openneuro.*) datasets fetchable via the snapshot manifest; Dryad manifest-only (token/bot-gated); Mendeley + other repos discovery-only.",
        id_example="datacite:10.5061/dryad.t4b8gtjgj",
    ),
    _spec(
        "dandi",
        dandi,
        layer="omics",
        kinds=("dataset",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (DANDI Archive search/resolve; asset-manifest fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Assets stream from the DANDI API (302→S3, unverified — no checksum in the listing); the manifest is capped at the first 100 assets for large dandisets.",
        id_example="dandi:000004",
        description="DANDI Archive — neurophysiology dandisets (NWB); search + resolve with a per-asset download manifest.",
    ),
    _spec(
        "omics",
        omics,
        layer="omics",
        kinds=("study", "sequencing_run"),
        filters_supported=(
            "query",
            "organism",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ),
        rate_limit="NCBI 3/s (10/s with NCBI_API_KEY); ENA unmetered",
        status="live (discovery; SRA FASTQ + GEO supplementary fetch on resolve)",
        fetchable="per-sub-source",
        fetchable_prefixes={"geo", "sra"},  # bioproject is routable but not fetchable
        fetchable_notes="SRA (ENA FASTQ, md5) + GEO supplementary fetchable; BioProject discovery-only (resolve attaches SRA-run links).",
        id_example="sra:SRX079566 | geo:GSE10072 | bioproject:PRJNA231221",
    ),
    _spec(
        "literature",
        literature,
        layer="literature",
        kinds=("publication",),
        filters_supported=(
            "query",
            "organism",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ),
        rate_limit="NCBI 3/s (10/s with NCBI_API_KEY); OpenAIRE + ScholeXplorer unmetered",
        status="live (discovery + resolve-time data links + identifiers; fetch retrieves open-access full text via EuropePMC/Unpaywall)",
        fetchable="open-access only",
        fetchable_notes="Open-access full text fetchable (EuropePMC XML / Unpaywall PDF, unverified); paywalled/non-OA ids fail loud.",
        id_example=("pubmed:23066504 | openaire:od______9773::3290080244992524e3fa0eba329e6122"),
    ),
    _spec(
        "huggingface",
        huggingface,
        layer="archives",
        kinds=("dataset",),
        filters_supported=(
            "query",
            "size",
            "published_after",
            "published_before",
            "kind",
            "cursor",
        ),
        rate_limit="HuggingFace Hub anonymous (generous)",
        status="live (discovery + resolve + fetch; contributes to page 1 only — HF paginates by cursor, not offset)",
        fetchable=True,
        operable=True,
        fetchable_notes="Files downloadable via the HF resolve URL (unverified — no checksum/size in the API).",
        id_example="hf:davidcechak/Arabidopsis_thaliana_DNA_v0",
        description="HuggingFace Hub datasets — searchable, resolvable, and fetchable via the resolve URL.",
    ),
    _spec(
        "omicsdi",
        omicsdi,
        layer="omics",
        kinds=("study",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (proteomics/metabolomics discovery; first page only)",
        fetchable="per-repo",
        fetchable_notes="PRIDE + MetaboLights records are fetchable (unverified - no upstream checksum); MassIVE/Metabolomics Workbench/GNPS/PeptideAtlas are discovery-only.",
        id_example="omicsdi:pride:PXD000001",
        description="Omics Discovery Index - proteomics & metabolomics studies; restricted to the mass-spec modality repos not already covered by the omics leg.",
    ),
    _spec(
        "openml",
        openml,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (name-substring discovery, first page only; ARFF + Parquet fetch on resolve)",
        fetchable=True,
        operable=True,
        fetchable_notes="ARFF fetch is md5-verified; the auto-converted Parquet is operable (schema/preview/head/sql).",
        id_example="openml:61",
        description="OpenML machine-learning datasets — name-substring search; resolve attaches an md5-verified ARFF and an operable Parquet.",
    ),
    _spec(
        "pdb",
        pdb,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (full-text discovery; .cif/.pdb structure fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Structure files (.cif/.pdb) stream from files.rcsb.org (unverified — no upstream checksum).",
        id_example="pdb:1BG2",
        description="RCSB Protein Data Bank — macromolecular structures; full-text search, DOI/PMID-rich, .cif/.pdb fetch.",
    ),
    _spec(
        "uniprot",
        uniprot,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (entry discovery; FASTA sequence fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="FASTA sequence streams from rest.uniprot.org (unverified — no upstream checksum).",
        id_example="uniprot:P01308",
        description="UniProtKB — protein sequences & functional annotation; full-text search, FASTA fetch.",
    ),
    _spec(
        "gwas",
        gwas,
        layer="omics",
        kinds=("study",),
        filters_supported=("query", "size"),
        rate_limit="public; courtesy only",
        status="live (disease-trait discovery; PubMed cross-link). Fetch not supported.",
        fetchable=False,
        fetchable_notes="Discovery-only: study metadata + PMID bridge. Summary-statistics fetch is a future wave.",
        id_example="gwas:GCST000028",
        description="GWAS Catalog (EBI) — genome-wide association studies keyed by disease trait; DOI/PMID-rich, reinforces the paper-data bridge. NOTE: query must be an exact GWAS Catalog disease-trait vocabulary term (e.g. 'Type 2 diabetes'), not free text — the EBI findByDiseaseTrait API performs case-insensitive exact trait matching.",
    ),
    _spec(
        "nasacmr",
        nasacmr,
        layer="archives",
        kinds=("dataset",),
        filters_supported=("query", "size", "cursor"),
        rate_limit="public Earthdata CMR; courtesy only",
        status="live (NASA Earthdata collection discovery; keyless)",
        fetchable=False,
        operable=False,
        fetchable_notes="Discovery-only: a collection has no single downloadable file - granule bytes live behind an Earthdata login (not wired). resolve carries the DOI + a data-access portal link.",
        id_example="nasacmr:C2586786218-POCLOUD",
        description="NASA CMR (Common Metadata Repository) - Earthdata earth-science collections (satellite, atmospheric, oceanographic, climate); DOI-normalized discovery.",
    ),
    _spec(
        "biostudies",
        biostudies,
        layer="omics",
        kinds=("study",),
        filters_supported=("query", "size", "offset"),
        rate_limit="public; courtesy only",
        status="live (free-text search across collections; file manifest + DOI/xref on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Study files stream from www.ebi.ac.uk/biostudies/files (302 -> FIRE). UNVERIFIED: the API publishes no md5/sha256 for study files, so fetch cannot check integrity here the way Zenodo/ENA fetches can.",
        id_example="biostudies:E-MTAB-12595",
        description="BioStudies (EBI) — functional-genomics studies including the ArrayExpress collection; EBI's counterpart to GEO. Resolve surfaces the file manifest, the publication DOI, and sibling accessions (GEO/ENA) that feed relate and cross-source dedup. NOTE: totalHits on the cross-collection search is an ESTIMATE (the API returns isTotalHitsExact=false); it is exact within a single collection.",
    ),
)

# Name → adapter module, in registration (precedence) order.
ADAPTERS: dict[str, SourceAdapter] = {s.name: s.module for s in SOURCES}

# Sources with no fetch backend at all — lowest DOI-dedup precedence (router._fetch_priority).
DISCOVERY_ONLY: frozenset[str] = frozenset(s.name for s in SOURCES if not s.fetchable_prefixes)

# Fetch-gate: id prefixes with a working fetch backend (server._is_fetchable checks startswith).
FETCHABLE_PREFIXES: tuple[str, ...] = tuple(
    f"{p}:" for s in SOURCES for p in sorted(s.fetchable_prefixes)
)

_BY_NAME: dict[str, SourceSpec] = {s.name: s for s in SOURCES}

# Presentation order for the list_sources catalog — historical (roughly the order sources were
# wired). Deliberately NOT SOURCES' order, which is dedup/merge precedence and load-bearing;
# keeping the two separate lets precedence change without churning a public tool payload.
CATALOG_ORDER: tuple[str, ...] = (
    "zenodo",
    "datacite",
    "omics",
    "literature",
    "huggingface",
    "dataone",
    "gbif",
    "datagov",
    "nasacmr",
    "omicsdi",
    "dandi",
    "openml",
    "pdb",
    "uniprot",
    "gwas",
    "biostudies",
    "cellxgene",
)

if set(CATALOG_ORDER) != set(_BY_NAME):  # a new source must be given a catalog position
    raise RuntimeError(
        "CATALOG_ORDER does not cover every registered source: "
        f"missing={sorted(set(_BY_NAME) - set(CATALOG_ORDER))!r} "
        f"unknown={sorted(set(CATALOG_ORDER) - set(_BY_NAME))!r}"
    )

# The list_sources payload (re-exported as server._SOURCES).
CATALOG: list[dict[str, Any]] = [_BY_NAME[name].catalog_entry() for name in CATALOG_ORDER]


def resolver_for(prefix: str) -> SourceAdapter | None:
    """The adapter module that resolves ids with ``prefix``, or None if unrouted.
    (Bare-numeric → zenodo and bare-DOI → datacite are handled by the caller.)"""
    for spec in SOURCES:
        if prefix in spec.prefixes:
            return spec.module
    return None
