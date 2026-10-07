"""Unified NCBI E-utils omics adapter (GEO + SRA + BioProject discovery).

Registered as a single ``omics`` source; ``search`` fans out across the three
NCBI databases internally and merges. Resolve attaches the ENA file manifest
to SRA records (sequencing runs → FASTQ) and the GEO ``suppl/`` directory
listing to GEO records. BioProject stays discovery-only (files=[]); its data
lives in linked SRA runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from defusedxml import ElementTree as ET  # remote XML: entity-expansion safe
from defusedxml.common import DefusedXmlException

from data_aggregator_mcp import _eutils, ena, geo
from data_aggregator_mcp._merge import fan_in, interleave
from data_aggregator_mcp._relevance import with_plurals
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError
from data_aggregator_mcp.models import DataResource, Link, compact

logger = logging.getLogger(__name__)

# friendly source/prefix → NCBI E-utils db
_DB = {"geo": "gds", "sra": "sra", "bioproject": "bioproject"}
PREFIXES = tuple(_DB)  # ("geo", "sra", "bioproject") — derived so it can't drift from _DB
DEFAULT_SIZE = 10
MAX_SIZE = 50
# esearch ANDs every word, so words that each match hundreds of records can match none
# together (GEO: "tardigrade" 221, "tun" 464, "tardigrade dehydration tun" 0; probed
# 2026-10-05); the router names these dbs when a multi-word search comes back empty.
REQUIRES_EVERY_WORD = True
# Each plain word goes with its plural: NCBI maps some plurals, never the singular, to a
# taxon, so "tardigrades" also matches Tardigrada records that never name the word and
# "tardigrade" does not. SRA "tardigrade anhydrobiosis" found 4 runs and none of the
# Ramazzottius varieornatus study PRJDB2359; "(tardigrade OR tardigrades) anhydrobiosis"
# found 37 with all six of its runs (2026-10-07). The OR form never counted fewer than the
# singular: "plants" adds Viridiplantae, "bees" Anthophila, "mouses" nothing.
QUERY_PLURALS = True

# Per-db accession search field. NOT uniform: the BioProject index has no ``ACCN``
# field at all (a term like ``PRJNA231221[ACCN]`` matches ZERO records there, while the
# same syntax is correct for gds/sra), so resolving any bioproject id used to fail with
# a flat NotFoundError. Keyed by db so a new db must state its own field.
_ACCESSION_FIELD = {"gds": "ACCN", "sra": "ACCN", "bioproject": "PRJA"}

# An accession search is a MATCH, not an identity lookup: GEO indexes every related
# accession under ACCN (``GSM613466[ACCN]`` hits the sample AND its parent series, which
# NCBI lists first; ``GPL570[ACCN]`` hits ~184k series run on that platform) and an SRA
# study/run matches each experiment that carries it. Resolve therefore restricts GEO to
# the entry type the accession names (GEO's ``ETYP`` values are the lowercase prefixes)
# and keeps only the candidate whose own id IS the requested one — never ``docs[0]``.
_GEO_ENTRY_TYPES = frozenset({"gse", "gsm", "gpl", "gds"})
_RESOLVE_CANDIDATES = 20

# Cap on SRA run links attached to a BioProject. elink is unbounded — real projects
# reach into the thousands (PRJNA231221 links 7,314 runs) — and every uid had to be
# turned into an accession through one esummary GET, which overflowed the URL length
# limit outright. A resolve payload carrying thousands of links is also unusable to a
# caller, so the list is bounded and the truncation is logged.
MAX_LINKED_RUNS = 100


class _Unreadable(Exception):
    """A summary field the adapter reads is missing or not of the type NCBI sends."""


def _text(doc: dict[str, Any], key: str) -> str:
    """``doc[key]`` as text; an absent field reads as "". Every field read from a summary
    goes through here, so a wrong-typed one is refused instead of escaping as a bare
    ``TypeError`` or pydantic error (all 1,400 live summaries sampled carry strings)."""
    value = doc.get(key, "")
    if not isinstance(value, str):
        raise _Unreadable(f"{key} is {type(value).__name__}, not text")
    return value


def _required(doc: dict[str, Any], key: str) -> str:
    value = _text(doc, key)
    if not value:
        raise _Unreadable(f"no {key}")
    return value


def _xml(doc: dict[str, Any], key: str) -> Any:
    # expxml/runs are XML *fragments* (multiple top-level elements) → wrap in a root.
    try:
        return ET.fromstring(f"<root>{_text(doc, key)}</root>")
    except (ET.ParseError, DefusedXmlException) as exc:
        raise _Unreadable(f"{key} is not XML ({exc})") from None


@contextmanager
def _reading(db: str, doc: dict[str, Any]) -> Iterator[None]:
    """Report a summary the adapter cannot read as upstream trouble, naming the db and uid.

    Not retried: the summaries come from ``_eutils.esummary``, which takes no ``check``."""
    try:
        yield
    except _Unreadable as exc:
        raise UpstreamUnavailableError(
            f"NCBI esummary ({db}) answered an unreadable summary for uid {doc.get('uid')!r}: {exc}"
        ) from None


def _year_from(text: str | None) -> int | None:
    if text and len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _normalize_geo(doc: dict[str, Any]) -> DataResource:
    acc = _required(doc, "accession")
    # GEO's ``taxon`` names every organism of the entry, joined by "; " ("Homo sapiens;
    # Mus musculus": 11 of 600 live gds summaries). Read as one name, the taxonomy
    # lookup matched only one of them and the record lost the others.
    organism = [name.strip() for name in _text(doc, "taxon").split(";") if name.strip()]
    return DataResource(
        id=f"geo:{acc}",
        source="geo",
        kind="study",
        title=_text(doc, "title"),
        year=_year_from(_text(doc, "pdat")),
        description=_text(doc, "summary") or None,
        accessions=[acc],
        organism=organism,
    )


def _normalize_sra(doc: dict[str, Any]) -> DataResource:
    """An SRA experiment. Its files (the ENA manifest) are attached at resolve."""
    exp = _xml(doc, "expxml")
    runs = _xml(doc, "runs")
    experiment = exp.find("Experiment")
    if experiment is None or not experiment.get("acc"):
        raise _Unreadable("expxml names no Experiment accession")
    exp_acc = experiment.attrib["acc"]
    study = exp.find("Study")
    organism = exp.find("Organism")
    bioproject = exp.findtext("Bioproject")
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
        year=_year_from(_text(doc, "createdate")),
        description=description,
        accessions=accessions,
        organism=[org_name] if org_name else [],
    )


def _normalize_bioproject(doc: dict[str, Any]) -> DataResource:
    acc = _required(doc, "project_acc")
    org = _text(doc, "organism_name")
    return DataResource(
        id=f"bioproject:{acc}",
        source="bioproject",
        kind="study",
        title=_text(doc, "project_title"),
        year=_year_from(_text(doc, "registration_date")),
        description=_text(doc, "project_description") or None,
        accessions=[acc],
        organism=[org] if org else [],
    )


_NORMALIZERS = {"gds": _normalize_geo, "sra": _normalize_sra, "bioproject": _normalize_bioproject}


def _normalize(db: str, doc: dict[str, Any]) -> DataResource:
    with _reading(db, doc):
        return _NORMALIZERS[db](doc)


async def _search_db(
    client: httpx.AsyncClient, db: str, query: str, size: int, offset: int
) -> tuple[int, list[DataResource]]:
    count, ids = await _eutils.esearch(client, db, query, retmax=size, retstart=offset)
    if not ids:
        return count, []
    docs = await _eutils.esummary(client, db, ids)
    return count, [_normalize(db, d) for d in docs]


async def _bioproject_sra_links(
    client: httpx.AsyncClient, bioproject_uid: str
) -> tuple[list[Link], str | None]:
    """Links to the SRA runs under a BioProject (elink bioproject→sra), each as a
    directly-resolvable ``sra:`` id, and a truncation note when capped. No edges → [].

    Bounded by ``MAX_LINKED_RUNS``: elink returns every run in the project, which for a
    large one is thousands of uids — more than a single esummary URL can carry, and more
    links than a resolve payload should hold. Truncation is logged AND returned as a note
    for the record's ``truncated["links"]``, never silent.
    """
    uids = await _eutils.elink(client, dbfrom="bioproject", db="sra", ids=[bioproject_uid])
    if not uids:
        return [], None
    note = None
    if len(uids) > MAX_LINKED_RUNS:
        note = (
            f"first {MAX_LINKED_RUNS} of {len(uids)} SRA runs; search sources=['omics'] "
            "for the project accession to page them all"
        )
        logger.warning(
            "BioProject uid=%s links %d SRA runs; attaching the first %d "
            "(search sources=['omics'] for the project accession to page them all)",
            bioproject_uid,
            len(uids),
            MAX_LINKED_RUNS,
        )
        uids = uids[:MAX_LINKED_RUNS]
    docs = await _eutils.esummary(client, "sra", uids)
    return [Link(rel="has_data", target_id=_normalize("sra", doc).id) for doc in docs], note


# The router pages each NCBI db as its own stream (``omics/geo`` ...) so each keeps its own
# offset and a failing db is reported by name. One shared offset applied to all three,
# then an interleave cut to ``size``, lost 60 of every 90 records across a cursor walk.
SUBSOURCES = PREFIXES


async def search_subsource(
    client: httpx.AsyncClient,
    subsource: str,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    plurals: bool = True,
) -> tuple[int, list[DataResource]]:
    """One NCBI db (``geo`` / ``sra`` / ``bioproject``) at its own offset, each plain word
    sent with its plural unless ``plurals`` is off. Raises on failure."""
    q = with_plurals(query) if plurals else query
    total, recs = await _search_db(client, _DB[subsource], q, min(size, MAX_SIZE), offset)
    return total, [compact(r) for r in recs]


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    plurals: bool = True,
) -> tuple[int, list[DataResource]]:
    """Discover across GEO + SRA + BioProject. Returns (summed_total, COMPACT).

    One page only: ``offset`` applies to every db, so this cannot walk a cursor — the
    router pages ``search_subsource`` per db instead. Raises when every db fails."""
    capped = min(size, MAX_SIZE)
    total, per_db = await fan_in(
        {
            sub: search_subsource(client, sub, query, size=capped, offset=offset, plurals=plurals)
            for sub in _DB
        },
        what="omics search",
        logger=logger,
    )
    return total, interleave(per_db)[:capped]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``geo:<acc>`` / ``sra:<acc>`` / ``bioproject:<acc>`` to a full record.

    Looks up the accession via esearch → esummary. For SRA, attaches the ENA
    filereport manifest (FASTQ files). For GEO, attaches the supplementary files
    listed under the record's ``ftplink`` ``suppl/`` directory (when present).
    BioProject stays files=[]; its data lives in linked SRA runs.

    Returns the record whose own id IS ``resource_id`` or raises NotFoundError — an
    SRA study/run id names the experiments that carry it instead of becoming one.
    """
    prefix, _, acc = resource_id.partition(":")
    db = _DB.get(prefix)
    if db is None or not acc:
        raise NotFoundError(f"unroutable omics id {resource_id!r}")
    term = f"{acc}[{_ACCESSION_FIELD[db]}]"
    entry_type = acc[:3].lower()
    if db == "gds" and entry_type in _GEO_ENTRY_TYPES:
        term += f" AND {entry_type}[ETYP]"
    count, ids = await _eutils.esearch(client, db, term, retmax=_RESOLVE_CANDIDATES)
    docs = await _eutils.esummary(client, db, ids)
    wanted = f"{prefix}:{acc}".lower()
    candidates = [(doc, _normalize(db, doc)) for doc in docs]
    matches = [(doc, rec) for doc, rec in candidates if rec.id.lower() == wanted]
    if not matches:
        # Candidates that merely CARRY the accession (an SRA study/run inside an
        # experiment) are named, never substituted: they are different records.
        parents = [
            rec.id for _, rec in candidates if acc.lower() in {a.lower() for a in rec.accessions}
        ]
        if parents:
            raise NotFoundError(
                f"{resource_id!r} is not itself a {prefix} record; it belongs to "
                f"{', '.join(parents)}"
                + (f" (first {len(docs)} of {count})" if count > len(docs) else "")
                + " — resolve one of those ids"
            )
        raise NotFoundError(f"no omics record for {acc!r} in {prefix}")
    doc, resource = matches[0]
    if prefix == "sra":
        files = await ena.filereport(client, resource.id.removeprefix("sra:"))
        if files:
            resource = resource.model_copy(update={"files": files})
    elif prefix == "geo":
        with _reading(db, doc):
            ftplink = _text(doc, "ftplink")
        files = await geo.supplementary_files(client, ftplink)
        if files:
            resource = resource.model_copy(update={"files": files})
    elif prefix == "bioproject":
        with _reading(db, doc):
            uid = _required(doc, "uid")
        links, note = await _bioproject_sra_links(client, uid)
        if links:
            resource = resource.model_copy(update={"links": links})
        if note:
            resource = resource.model_copy(update={"truncated": {"links": note}})
    return resource
