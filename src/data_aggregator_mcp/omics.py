"""Unified NCBI E-utils omics adapter (GEO + SRA + BioProject discovery).

Registered as a single ``omics`` source; ``search`` fans out across the three
NCBI databases internally and merges. Resolve attaches the ENA file manifest
to SRA records (sequencing runs → FASTQ) and the GEO ``suppl/`` directory
listing to GEO records. BioProject stays discovery-only (files=[]); its data
lives in linked SRA runs.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from data_aggregator_mcp import _eutils, ena, geo
from data_aggregator_mcp._merge import interleave
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, Link, compact

logger = logging.getLogger(__name__)

# friendly source/prefix → NCBI E-utils db
_DB = {"geo": "gds", "sra": "sra", "bioproject": "bioproject"}
PREFIXES = tuple(_DB)  # ("geo", "sra", "bioproject") — derived so it can't drift from _DB
DEFAULT_SIZE = 10
MAX_SIZE = 50


def _year_from(text: str | None) -> int | None:
    if text and len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _normalize_geo(doc: dict[str, Any]) -> DataResource:
    acc = doc.get("accession", "")
    organism = [doc["taxon"]] if doc.get("taxon") else []
    return DataResource(
        id=f"geo:{acc}",
        source="geo",
        kind="study",
        title=doc.get("title", ""),
        year=_year_from(doc.get("pdat")),
        description=doc.get("summary") or None,
        accessions=[acc] if acc else [],
        organism=organism,
        files=[],
    )


def _normalize_sra(doc: dict[str, Any]) -> DataResource:
    # expxml/runs are XML *fragments* (multiple top-level elements) → wrap in a root.
    exp = ET.fromstring(f"<root>{doc.get('expxml', '')}</root>")
    runs = ET.fromstring(f"<root>{doc.get('runs', '')}</root>")
    experiment = exp.find("Experiment")
    study = exp.find("Study")
    organism = exp.find("Organism")
    bioproject = exp.findtext("Bioproject")
    exp_acc = experiment.get("acc") if experiment is not None else ""
    study_acc = study.get("acc") if study is not None else None
    study_name = study.get("name") if study is not None else None
    summary_title = exp.findtext("Summary/Title")
    org_name = organism.get("ScientificName") if organism is not None else None
    run_accs = [r.get("acc") for r in runs.findall("Run") if r.get("acc")]

    accessions = [a for a in [exp_acc, study_acc, bioproject, *run_accs] if a]
    title = study_name or summary_title or ""
    # SRA expxml carries no abstract; when the study name takes the title slot,
    # surface the experiment-level Summary/Title as description so it isn't lost.
    description = summary_title if summary_title and summary_title != title else None
    return DataResource(
        id=f"sra:{exp_acc}",
        source="sra",
        kind="sequencing_run",
        title=title,
        year=_year_from(doc.get("createdate")),
        description=description,
        accessions=accessions,
        organism=[org_name] if org_name else [],
        files=[],  # ENA manifest attached at resolve
    )


def _normalize_bioproject(doc: dict[str, Any]) -> DataResource:
    acc = doc.get("project_acc", "")
    org = doc.get("organism_name")
    return DataResource(
        id=f"bioproject:{acc}",
        source="bioproject",
        kind="study",
        title=doc.get("project_title", ""),
        year=_year_from(doc.get("registration_date")),
        description=doc.get("project_description") or None,
        accessions=[acc] if acc else [],
        organism=[org] if org else [],
        files=[],
    )


_NORMALIZERS = {"gds": _normalize_geo, "sra": _normalize_sra, "bioproject": _normalize_bioproject}


async def _search_db(
    client: httpx.AsyncClient, db: str, query: str, size: int
) -> tuple[int, list[DataResource]]:
    count, ids = await _eutils.esearch(client, db, query, retmax=size)
    if not ids:
        return count, []
    docs = await _eutils.esummary(client, db, ids)
    normalize = _NORMALIZERS[db]
    return count, [normalize(d) for d in docs]


async def _bioproject_sra_links(client: httpx.AsyncClient, bioproject_uid: str) -> list[Link]:
    """Links to the SRA runs under a BioProject (elink bioproject→sra), each as a
    directly-resolvable ``sra:`` id. No edges → []."""
    uids = await _eutils.elink(client, dbfrom="bioproject", db="sra", ids=[bioproject_uid])
    if not uids:
        return []
    docs = await _eutils.esummary(client, "sra", uids)
    return [Link(rel="has_data", target_id=_normalize_sra(doc).id) for doc in docs]


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
) -> tuple[int, list[DataResource]]:
    """Discover across GEO + SRA + BioProject. Returns (summed_total, COMPACT)."""
    capped = min(size, MAX_SIZE)
    outcomes = await asyncio.gather(
        *(_search_db(client, db, query, capped) for db in _DB.values()),
        return_exceptions=True,
    )
    total = 0
    per_db: list[list[DataResource]] = []
    for db, outcome in zip(_DB.values(), outcomes):
        if isinstance(outcome, Exception):
            # partial results beat total failure, but log (don't silently swallow) the cause
            logger.warning("omics search: NCBI %s db failed: %r", db, outcome)
            continue
        db_total, recs = outcome
        total += db_total
        per_db.append(recs)
    merged = interleave(per_db)[:capped]
    return total, [compact(r) for r in merged]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``geo:<acc>`` / ``sra:<acc>`` / ``bioproject:<acc>`` to a full record.

    Looks up the accession via esearch → esummary. For SRA, attaches the ENA
    filereport manifest (FASTQ files). For GEO, attaches the supplementary files
    listed under the record's ``ftplink`` ``suppl/`` directory (when present).
    BioProject stays files=[]; its data lives in linked SRA runs.
    """
    prefix, _, acc = resource_id.partition(":")
    db = _DB.get(prefix)
    if db is None or not acc:
        raise NotFoundError(f"unroutable omics id {resource_id!r}")
    _count, ids = await _eutils.esearch(client, db, f"{acc}[ACCN]", retmax=1)
    if not ids:
        raise NotFoundError(f"no omics record for {acc!r} in {prefix}")
    docs = await _eutils.esummary(client, db, ids)
    if not docs:
        raise NotFoundError(f"no omics record for {acc!r} in {prefix}")
    resource = _NORMALIZERS[db](docs[0])
    if prefix == "sra":
        files = await ena.filereport(client, acc)
        if files:
            resource = resource.model_copy(update={"files": files})
    elif prefix == "geo":
        ftplink = docs[0].get("ftplink") or ""
        files = await geo.supplementary_files(client, ftplink)
        if files:
            resource = resource.model_copy(update={"files": files})
    elif prefix == "bioproject":
        links = await _bioproject_sra_links(client, ids[0])
        if links:
            resource = resource.model_copy(update={"links": links})
    return resource
