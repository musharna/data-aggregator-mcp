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
    _pushdown,
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
    # Derived from the adapter, never written by hand (``_filters_supported``).
    filters_supported: tuple[str, ...]
    rate_limit: str
    status: str
    id_example: str
    fetchable: bool | str
    auth_required: bool = False
    operable: bool | None = None
    fetchable_notes: str | None = None
    description: str | None = None
    # A licence the SOURCE dedicates its whole archive under, for sources whose records
    # carry no licence of their own. Set ONLY where the operator publishes a blanket
    # policy that admits no per-record exceptions, and always with the citation in
    # ``default_license_policy`` — this is a claim about someone else's data, so an
    # unsourced guess here is worse than the "unknown" it replaces. It never overrides a
    # licence the record does state; see ``license_compat.check``.
    default_license: str | None = None
    default_license_policy: str | None = None
    # Whether the upstream search parses the router's ontology expansion
    # ``(q) AND ("a" OR "b")``. A keyword-only upstream answers it with 0 hits (it
    # substring-matches the literal) or an HTTP 400, so the router sends it the plain query.
    boolean_query: bool = True

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


# The router's ontology facets: each expands the query with the term's synonyms, so a
# source sees them exactly when it is sent the expanded query (``boolean_query``).
ONTOLOGY_FACETS = ("organism", "disease", "tissue", "chemical", "assay")
# One bound per year filter, to ask an adapter's ``pushable`` which filters it takes.
_YEAR_PROBE = dict.fromkeys(_pushdown.YEAR_FILTERS, 2000)


def _filters_supported(
    module: SourceAdapter, kinds: tuple[str, ...], *, boolean_query: bool
) -> tuple[str, ...]:
    """The ``search`` parameters this source applies itself, read off its adapter. Written
    by hand, the list drifted from the code in both directions (#200 triage, 2026-09-30):
    huggingface listed ``cursor`` and the year/kind filters but serves page 1 only and
    filters nothing upstream; omics and literature listed year/kind they filter only after
    fetch; DataCite omitted ``size``; five paging sources omitted ``cursor``; biostudies
    listed ``offset``, which ``search`` does not take; only omics and literature listed
    ``organism``, which every boolean-query source gets.

    - ``query``, ``size``: every source.
    - ``cursor``: the source returns records past its first page (``PAGINATES``, default
      True; a page-1-only adapter sets it False beside its ``if offset`` guard).
    - ``published_after`` / ``published_before`` / ``kind``: the source filters upstream
      (``_pushdown.FilterPushdown``), so its total is the filtered one; ``kind`` only when
      every kind the source carries can be pushed. Any other source is filtered after fetch.
    - the ontology facets: the source is sent the expanded query (``boolean_query``).
    """
    out = ["query", "size"]
    if getattr(module, "PAGINATES", True):
        out.append("cursor")
    if isinstance(module, _pushdown.FilterPushdown):
        out += [k for k in _pushdown.YEAR_FILTERS if k in module.pushable(_YEAR_PROBE)]
        own = [k for k in kinds if k != _pushdown.OTHER_KIND]
        if own and all("kind" in module.pushable({"kind": k}) for k in own):
            out.append("kind")
    if boolean_query:
        out += ONTOLOGY_FACETS
    return tuple(out)


def _spec(
    name: str,
    module: SourceAdapter,
    *,
    layer: str,
    kinds: tuple[str, ...],
    rate_limit: str,
    status: str,
    id_example: str,
    fetchable: bool | str = True,
    fetchable_prefixes: Collection[str] | None = None,
    auth_required: bool = False,
    operable: bool | None = None,
    fetchable_notes: str | None = None,
    description: str | None = None,
    default_license: str | None = None,
    default_license_policy: str | None = None,
    boolean_query: bool = True,
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
        filters_supported=_filters_supported(module, kinds, boolean_query=boolean_query),
        rate_limit=rate_limit,
        status=status,
        id_example=id_example,
        fetchable=fetchable,
        auth_required=auth_required,
        operable=operable,
        fetchable_notes=fetchable_notes,
        description=description,
        default_license=default_license,
        default_license_policy=default_license_policy,
        boolean_query=boolean_query,
    )


# Order = _dedup merge precedence: fetchable natives before DataCite (so on a shared DOI the
# fetchable copy is seen first); discovery-only sources (gwas/nasacmr) registered late so a
# fetchable native still wins. See router._fetch_priority for the collision rule itself.
SOURCES: tuple[SourceSpec, ...] = (
    _spec(
        "zenodo",
        zenodo,
        layer="archives",
        kinds=("dataset", "publication", "software", "other"),
        rate_limit="~60/min anonymous",
        status="live",
        fetchable=True,
        operable=True,
        # A record id, open, with files: 7654321 was a concept id that redirected to a
        # restricted, fileless "Incorrect upload" (test_live_every_advertised_id_example_...).
        id_example="zenodo:1254563",
    ),
    _spec(
        "dataone",
        dataone,
        layer="archives",
        kinds=("dataset",),
        rate_limit="public CN; courtesy only",
        status="live (eco/environmental federation; verified fetch via Member Nodes)",
        fetchable=True,
        operable=True,
        fetchable_notes="Data objects fetched from Member Nodes with per-object MD5/SHA-256 verification.",
        id_example="dataone:doi:10.18739/A26336",
        # Deliberately NO default_license. DataONE is a FEDERATION — each Member Node sets
        # its own terms — so there is no operator-level policy to cite. Its Solr index has
        # no licence field at all (`licenseName` / `licenseUrl` return "undefined field"),
        # only `rightsHolder`, which is populated on 100% of ~3.36M records but names the
        # rights HOLDER, not a grant. Reading it as a licence would be a fabrication.
        description="DataONE federation of environmental & earth-science repositories (KNB, Arctic Data Center, PANGAEA, TERN, ...).",
    ),
    # GBIF DOIs share the 10.15468 DataCite prefix — the native must precede datacite.
    _spec(
        "gbif",
        gbif,
        layer="archives",
        kinds=("dataset",),
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
        rate_limit="keyless catalog API by default; with DATA_GOV_API_KEY, the api.data.gov gateway (1,000/hour per key)",
        status="live (US government open-data catalog; DCAT-US Catalog API, cursor-paged; search total is a lower bound - the API reports no hit count)",
        fetchable="per-dataset",
        operable=False,
        fetchable_notes="DCAT distributions fetched by direct URL (downloadURL, else accessURL; unverified - no upstream checksum); datasets without a distribution URL are discovery-only.",
        id_example="datagov:civil-rights-data-collection-crdc",
        description="data.gov - the US government open-data catalog (climate, agriculture, economic, civic & scientific datasets); non-biological breadth beyond the omics core.",
    ),
    _spec(
        "cellxgene",
        cellxgene,
        boolean_query=False,
        layer="omics",
        kinds=("dataset",),
        rate_limit="public; courtesy only",
        status="live (CZ CELLxGENE Discover collections search/resolve; asset manifest on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="H5AD/RDS assets stream from datasets.cellxgene.cziscience.com (direct URLs, unverified — no checksum in the API); the per-collection manifest is capped at 200 files for large atlases.",
        id_example="cellxgene:af893e86-8e9f-41f1-a474-ef05359b1fb7",
        description="CZ CELLxGENE Discover — single-cell datasets grouped by collection (one publication DOI per collection); search filters on tissue/disease/organism/assay, resolve attaches the H5AD/RDS download manifest.",
        # The curation API exposes NO licence field anywhere — verified across all 386
        # published collections and their nested dataset objects, not a single sample — so
        # every cellxgene record is silent and this default always applies. CZI publishes
        # the licence unilaterally as a condition of submission ("anyone will be able to
        # access it subject to a CC-BY 4.0 license"); contributors do not choose it, which
        # is what separates this from the gwas case below.
        default_license="CC-BY-4.0",
        default_license_policy=(
            "CZ CELLxGENE Discover publishing policy — "
            "https://cellxgene.cziscience.com/docs/032__Contribute%20and%20Publish%20Data"
        ),
    ),
    _spec(
        "datacite",
        datacite,
        layer="archives",
        kinds=("dataset", "publication", "software", "other"),
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
        # Live probe 2026-09-27: "mouse" 325 hits, the neutral expansion
        # (mouse) AND ("mouse" OR "mouse") 0 — any organism/ontology param zeroed DANDI.
        boolean_query=False,
        layer="omics",
        kinds=("dataset",),
        rate_limit="public; courtesy only",
        status="live (DANDI Archive search/resolve; asset-manifest fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Assets stream from the DANDI API (302→S3), sha-256-verified where DANDI has computed the hash; the manifest is capped at the first 100 assets for large dandisets.",
        id_example="dandi:000004",
        description="DANDI Archive — neurophysiology dandisets (NWB); search + resolve with a per-asset download manifest.",
    ),
    _spec(
        "omics",
        omics,
        layer="omics",
        kinds=("study", "sequencing_run"),
        # Deliberately NO default_license, and this is the clearest case of the four. NCBI
        # does not merely stay silent, it disclaims the ability to grant: it "places no
        # restrictions on the use or distribution of the data", but "some submitters of the
        # original data ... may claim patent, copyright, or other intellectual property
        # rights", and since "there is no transfer of rights from submitters to NCBI, NCBI
        # has no rights to transfer to a third party". An operator saying it cannot license
        # the data is the strongest possible reason not to default one.
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
        rate_limit="NCBI 3/s (10/s with NCBI_API_KEY); OpenAIRE + ScholeXplorer unmetered",
        status="live (discovery + resolve-time data links + identifiers; fetch retrieves open-access full text via EuropePMC/Unpaywall)",
        fetchable="open-access only",
        fetchable_notes="Open-access full text fetchable (EuropePMC XML / Unpaywall PDF, unverified); paywalled/non-OA ids fail loud.",
        id_example=("pubmed:23066504 | openaire:od______9773::3290080244992524e3fa0eba329e6122"),
    ),
    _spec(
        "huggingface",
        huggingface,
        boolean_query=False,
        layer="archives",
        kinds=("dataset",),
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
        rate_limit="public; courtesy only",
        status="live (proteomics/metabolomics discovery)",
        fetchable="per-repo",
        fetchable_notes="PRIDE records are fetchable (unverified - no upstream checksum); MetaboLights records are fetchable and sha-256-verified; MassIVE/jPOST/iProX/PeptideAtlas/Panorama Public/Metabolomics Workbench/GNPS are discovery-only.",
        id_example="omicsdi:pride:PXD000001",
        # Deliberately NO default_license, for the same reason as dataone: OmicsDI is an
        # INDEX over other repositories (PRIDE, MetaboLights, MassIVE, GNPS, ...), so the
        # terms belong to whichever repo the record came from — the `source` field says
        # which. Its search payload carries no rights key of any kind.
        description="Omics Discovery Index - proteomics & metabolomics studies; restricted to the mass-spec modality repos not already covered by the omics leg.",
    ),
    _spec(
        "openml",
        openml,
        boolean_query=False,
        layer="archives",
        kinds=("dataset",),
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
        rate_limit="public; courtesy only",
        status="live (full-text discovery; .cif/.pdb structure fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Structure files (.cif/.pdb) stream from files.rcsb.org (unverified — no upstream checksum).",
        id_example="pdb:1BG2",
        description="RCSB Protein Data Bank — macromolecular structures; full-text search, DOI/PMID-rich, .cif/.pdb fetch.",
        # PDB entries carry no licence field at all (verified: data.rcsb.org core/entry has
        # none), but the wwPDB dedicates the whole archive to CC0, and RCSB states data from
        # its programmatic APIs are under the same terms.
        default_license="CC0-1.0",
        default_license_policy="wwPDB usage policy — https://www.wwpdb.org/about/usage-policies",
    ),
    _spec(
        "uniprot",
        uniprot,
        layer="archives",
        kinds=("dataset",),
        rate_limit="public; courtesy only",
        status="live (entry discovery; FASTA sequence fetch on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="FASTA sequence streams from rest.uniprot.org (unverified — no upstream checksum).",
        id_example="uniprot:P01308",
        description="UniProtKB — protein sequences & functional annotation; full-text search, FASTA fetch.",
        # Every UniProtKB flat-file record states it in-band: "Distributed under the Creative
        # Commons Attribution (CC BY 4.0) License" (verified against rest.uniprot.org). The
        # JSON our adapter reads drops that notice, which is the only reason it needs a default.
        default_license="CC-BY-4.0",
        default_license_policy="UniProt licence — https://www.uniprot.org/help/license",
    ),
    _spec(
        "gwas",
        gwas,
        # The v2 disease_trait filter is exact trait matching (as v1's findByDiseaseTrait
        # was): the boolean expansion is no trait (live probe 2026-09-27 on v1: "Type 2
        # diabetes" 148 hits, expanded 0; re-checked on v2 by test_sources' live probe).
        boolean_query=False,
        layer="omics",
        kinds=("study",),
        rate_limit="public; courtesy only",
        status="live (disease-trait discovery; PubMed cross-link). Fetch not supported.",
        fetchable=False,
        fetchable_notes="Discovery-only: study metadata + PMID bridge. Summary-statistics fetch is a future wave.",
        id_example="gwas:GCST000028",
        # Deliberately NO default_license. The GWAS Catalog is mostly CC0 or EMBL-EBI
        # standard terms, but "with a small number of exceptions" — individual studies carry
        # their own Usage License. A blanket default would be wrong for precisely the records
        # where the licence matters, so these stay honestly unknown.
        description="GWAS Catalog (EBI) — genome-wide association studies keyed by disease trait; DOI/PMID-rich, reinforces the paper-data bridge. NOTE: query must be an exact GWAS Catalog disease-trait vocabulary term (e.g. 'Type 2 diabetes'), not free text — the GWAS Catalog REST API v2 disease_trait filter performs case-insensitive exact trait matching.",
    ),
    _spec(
        "nasacmr",
        nasacmr,
        boolean_query=False,
        layer="archives",
        kinds=("dataset",),
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
        rate_limit="public; courtesy only",
        status="live (free-text search across collections; file manifest + DOI/xref on resolve)",
        fetchable=True,
        operable=False,
        fetchable_notes="Study files stream from www.ebi.ac.uk/biostudies/files (302 -> FIRE). UNVERIFIED: the API publishes no md5/sha256 for study files, so fetch cannot check integrity here the way Zenodo/ENA fetches can.",
        id_example="biostudies:E-MTAB-12595",
        # Deliberately NO default_license. Studies carry no licence attribute (checked
        # across S-BSST/S-BIAD/E-MTAB submissions), but EMBL-EBI's terms are explicitly NOT
        # a grant: it "places no additional restrictions on the use or redistribution of
        # the data ... other than those provided by the original data owners", and
        # per-resource terms prevail on conflict. "No additional restrictions" is not the
        # same as permission, so there is nothing here to default to.
        description="BioStudies (EBI) — functional-genomics studies including the ArrayExpress collection; EBI's counterpart to GEO. Resolve surfaces the file manifest, the publication DOI, and sibling accessions (GEO/ENA) that feed relate and cross-source dedup. NOTE: totalHits on the cross-collection search is an ESTIMATE (the API returns isTotalHitsExact=false); it is exact within a single collection.",
    ),
)

# Name → adapter module, in registration (precedence) order.
ADAPTERS: dict[str, SourceAdapter] = {s.name: s.module for s in SOURCES}

# Source name → (blanket licence, policy citation), for the few sources that publish one.
# Derived from the registry rows so the citation cannot drift away from the licence it backs.
DEFAULT_LICENSES: dict[str, tuple[str, str]] = {
    s.name: (s.default_license, s.default_license_policy or "")
    for s in SOURCES
    if s.default_license
}


def default_license_for(source: str | None) -> tuple[str | None, str | None]:
    """The source's blanket licence and its citation, or ``(None, None)``. Unknown source
    names answer ``(None, None)`` rather than raising — this only ever adds information."""
    if not source:
        return None, None
    lic, policy = DEFAULT_LICENSES.get(source, (None, None))
    return lic, policy or None


# Sources with no fetch backend at all — lowest DOI-dedup precedence (router._fetch_priority).
DISCOVERY_ONLY: frozenset[str] = frozenset(s.name for s in SOURCES if not s.fetchable_prefixes)

# Fetch-gate: id prefixes with a working fetch backend (server._is_fetchable checks startswith).
FETCHABLE_PREFIXES: tuple[str, ...] = tuple(
    f"{p}:" for s in SOURCES for p in sorted(s.fetchable_prefixes)
)

_BY_NAME: dict[str, SourceSpec] = {s.name: s for s in SOURCES}

# Sources whose search cannot parse the boolean ontology expansion (see SourceSpec).
KEYWORD_ONLY: frozenset[str] = frozenset(s.name for s in SOURCES if not s.boolean_query)

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
