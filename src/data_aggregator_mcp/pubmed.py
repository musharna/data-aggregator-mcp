"""PubMed literature backend.

Discovery via NCBI E-utils (``esearch``/``esummary`` db=pubmed). Resolve attaches
data links via ``elink`` (pubmed → sra/gds/bioproject); each elinked record is run
through the omics normalizers so the link target is a directly-resolvable
``sra:``/``geo:``/``bioproject:`` id; the accessions Europe PMC mines from the text are
added as ``references`` links (``europepmc``). PubMed esummary carries no abstract, so
``resolve`` enriches ``description`` by fetching the article AbstractText(s) via
``efetch`` (best-effort; description stays None on any failure).
"""

from __future__ import annotations

import logging
import re

import httpx
from defusedxml import ElementTree as ET  # remote XML: entity-expansion safe

from data_aggregator_mcp import _eutils, europepmc, fulltext, omics
from data_aggregator_mcp.errors import NotFoundError, UpstreamUnavailableError, ValidationError
from data_aggregator_mcp.models import Creator, DataResource, Link

logger = logging.getLogger(__name__)

DEFAULT_SIZE = 10
MAX_SIZE = 50
# PubMed's esearch serves only the first 9,999 hits of a query: a retstart above 9998 is
# answered with an ERROR inside a 200 whose raw newline makes it invalid JSON (live
# 2026-10-02), so it was retried and read as an outage.
_LAST_OFFSET = 9998
# elink target dbs, in our canonical-prefix order; each reuses an omics normalizer.
_ELINK_DBS = ("sra", "gds", "bioproject")
# E-utilities read ``id`` as a comma-separated LIST, so a PMID is checked whole before it
# is sent: ``pubmed:34320281,1`` resolved one paper carrying the other's data links.
_PMID = re.compile(r"[0-9]+")


def _has(d: dict, key: str, kind: type) -> bool:
    """``key`` is absent from ``d`` or holds a ``kind``."""
    return key not in d or isinstance(d[key], kind)


def _is_doc(doc: dict) -> bool:
    """Every field ``_normalize_pubmed`` reads, at the type it reads it as. ``_eutils``
    checks only that each uid has an object. Every live DocSum has a PMID ``uid`` and a
    ``title`` (a whole book's is ``""``); the lists are checked when present."""
    uid = doc.get("uid")
    authors = doc.get("authors", [])
    articleids = doc.get("articleids", [])
    return (
        isinstance(uid, str)
        and _PMID.fullmatch(uid) is not None
        and isinstance(doc.get("title"), str)
        and _has(doc, "sortpubdate", str)
        and isinstance(authors, list)
        and all(isinstance(a, dict) and _has(a, "name", str) for a in authors)
        and isinstance(articleids, list)
        and all(
            isinstance(a, dict) and _has(a, "idtype", str) and _has(a, "value", str)
            for a in articleids
        )
    )


def _normalize_pubmed(doc: dict) -> DataResource:
    if not _is_doc(doc):
        raise UpstreamUnavailableError(
            f"NCBI esummary (pubmed) returned a malformed record: {doc!r:.200}"
        )
    uid = doc["uid"]
    articleids = doc.get("articleids", [])

    def _aid(idtype: str) -> str | None:
        return next(
            (a["value"] for a in articleids if a.get("idtype") == idtype and a.get("value")),
            None,
        )

    doi = _aid("doi")
    pmcid = _aid("pmc")  # clean "PMC..." (NOT the "pmcid" idtype, which is "pmc-id: PMC..;")
    identifiers = {k: v for k, v in (("pmid", uid), ("doi", doi), ("pmcid", pmcid)) if v}
    return DataResource(
        id=f"pubmed:{uid}",
        source="pubmed",
        kind="publication",
        title=doc["title"],
        creators=[Creator(name=a["name"]) for a in doc.get("authors", []) if a.get("name")],
        year=omics._year_from(doc.get("sortpubdate")),
        doi=doi,
        identifiers=identifiers,
    )


async def _abstract_for(client: httpx.AsyncClient, pmid: str) -> tuple[str | None, str | None]:
    """Fetch + join the article AbstractText(s), and why that failed (None when it did
    not). Enrichment: never raises, but a failure is returned next to the None so it is
    never indistinguishable from "this article has no abstract"."""
    try:
        xml_text = await _eutils.efetch(client, "pubmed", [pmid])
        if not xml_text:
            return None, None
        root = ET.fromstring(xml_text)
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise (spec §8)
        logger.warning("pubmed abstract fetch failed for %r: %r", pmid, exc)
        return None, f"NCBI efetch (abstract) failed: {type(exc).__name__}: {exc}"
    parts = [
        t.strip() for t in ("".join(el.itertext()) for el in root.iter("AbstractText")) if t.strip()
    ]
    return " ".join(parts) or None, None


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
) -> tuple[int, list[DataResource]]:
    if offset > _LAST_OFFSET:
        raise ValidationError(
            f"PubMed returns only the first {_LAST_OFFSET + 1} records of a search; "
            f"offset {offset} is past them, so narrow the query"
        )
    count, ids = await _eutils.esearch(
        client, "pubmed", query, retmax=min(size, MAX_SIZE), retstart=offset
    )
    docs = await _eutils.esummary(client, "pubmed", ids)
    return count, [_normalize_pubmed(d) for d in docs]


async def _links_via_elink(client: httpx.AsyncClient, pmid: str) -> tuple[list[Link], str | None]:
    """Discover data the paper links to via NCBI elink (pubmed → sra/gds/bioproject),
    and a truncation note when a db's links were capped.

    Each elinked uid is run through the matching omics normalizer so the link
    target is exactly the canonical id the omics adapter resolves
    (``sra:SRX…`` / ``geo:GSE…`` / ``bioproject:PRJNA…``).

    Bounded per db by ``omics.MAX_LINKED_RUNS``, as BioProject → SRA is: a paper can link
    thousands of SRA records (PMID 42544407: 7,498), and NCBI summarises at most 500 uids
    per JSON esummary and refuses a URL holding 2,000 (HTTP 414), so such a paper did not
    resolve at all.
    """
    out: list[Link] = []
    cut: list[str] = []
    for db in _ELINK_DBS:
        uids = await _eutils.elink(client, dbfrom="pubmed", db=db, ids=[pmid])
        if len(uids) > omics.MAX_LINKED_RUNS:
            cut.append(f"first {omics.MAX_LINKED_RUNS} of {len(uids)} linked {db} records")
        docs = await _eutils.esummary(client, db, uids[: omics.MAX_LINKED_RUNS])
        normalize = omics._NORMALIZERS[db]
        out.extend(Link(rel="has_data", target_id=normalize(doc).id) for doc in docs)
    return out, "; ".join(cut) or None


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``pubmed:<PMID>`` to a full record with elink-discovered data links
    and (when open access) an attached full-text file."""
    prefix, _, pmid = resource_id.partition(":")
    if prefix != "pubmed":
        raise NotFoundError(f"unroutable pubmed id {resource_id!r}")
    if not _PMID.fullmatch(pmid):
        # NCBI answers a non-number with an in-band error, which was retried and read as
        # an outage.
        raise NotFoundError(f"no pubmed record for {pmid!r}: a PMID is a decimal number")
    docs = await _eutils.esummary(client, "pubmed", [pmid])
    if not docs:
        raise NotFoundError(f"no pubmed record for {pmid!r}")
    resource = _normalize_pubmed(docs[0])
    links, links_cut = await _links_via_elink(client, pmid)
    mined, mined_cut, mined_error = await europepmc.mined_links(client, pmid=pmid)
    links = europepmc.merge(links, mined)
    ft = await fulltext.find(client, pmcid=resource.identifiers.get("pmcid"), doi=resource.doi)
    abstract, abstract_error = await _abstract_for(client, pmid)
    update: dict = {}
    # Each failed lookup is recorded, so the router does not cache a degraded record.
    errors = {**resource.errors}
    if ft.error:
        errors["files"] = ft.error
    if abstract_error:
        errors["description"] = abstract_error
    if mined_error:
        errors["links"] = mined_error
    if errors != resource.errors:
        update["errors"] = errors
    if links:
        update["links"] = links
    cuts = "; ".join(c for c in (links_cut, mined_cut) if c)
    if cuts:
        update["truncated"] = {"links": cuts}
    if ft.file is not None:
        update["files"] = [ft.file]
    if ft.access:
        update["access"] = ft.access
    if ft.license:
        update["license"] = ft.license
    if abstract:
        update["description"] = abstract
    if update:
        resource = resource.model_copy(update=update)
    return resource
