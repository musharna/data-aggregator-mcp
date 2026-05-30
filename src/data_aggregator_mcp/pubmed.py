"""PubMed literature backend.

Discovery via NCBI E-utils (``esearch``/``esummary`` db=pubmed). Resolve attaches
data links via ``elink`` (pubmed → sra/gds/bioproject); each elinked record is run
through the omics normalizers so the link target is a directly-resolvable
``sra:``/``geo:``/``bioproject:`` id. PubMed esummary carries no abstract, so
``resolve`` enriches ``description`` by fetching the article AbstractText(s) via
``efetch`` (best-effort; description stays None on any failure).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

from data_aggregator_mcp import _eutils, fulltext, omics
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.models import DataResource, Link

logger = logging.getLogger(__name__)

DEFAULT_SIZE = 10
MAX_SIZE = 50
# elink target dbs, in our canonical-prefix order; each reuses an omics normalizer.
_ELINK_DBS = ("sra", "gds", "bioproject")


def _normalize_pubmed(doc: dict) -> DataResource:
    uid = doc.get("uid", "")
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
        title=doc.get("title", ""),
        creators=[a["name"] for a in doc.get("authors", []) if a.get("name")],
        year=omics._year_from(doc.get("sortpubdate")),
        doi=doi,
        identifiers=identifiers,
        description=None,
        files=[],
    )


async def _abstract_for(client: httpx.AsyncClient, pmid: str) -> str | None:
    """Fetch + join the article AbstractText(s). Best-effort: any failure → None."""
    try:
        xml_text = await _eutils.efetch(client, "pubmed", [pmid], retmode="xml")
        if not xml_text:
            return None
        root = ET.fromstring(xml_text)
        parts = [
            t.strip()
            for t in ("".join(el.itertext()) for el in root.iter("AbstractText"))
            if t.strip()
        ]
        return " ".join(parts) or None
    except Exception as exc:  # noqa: BLE001 — enrichment: never raise (spec §8)
        logger.warning("pubmed abstract fetch failed for %r: %r", pmid, exc)
        return None


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
) -> tuple[int, list[DataResource]]:
    count, ids = await _eutils.esearch(client, "pubmed", query, retmax=min(size, MAX_SIZE))
    if not ids:
        return count, []
    docs = await _eutils.esummary(client, "pubmed", ids)
    return count, [_normalize_pubmed(d) for d in docs]


async def _links_via_elink(client: httpx.AsyncClient, pmid: str) -> list[Link]:
    """Discover data the paper links to via NCBI elink (pubmed → sra/gds/bioproject).

    Each elinked uid is run through the matching omics normalizer so the link
    target is exactly the canonical id the omics adapter resolves
    (``sra:SRX…`` / ``geo:GSE…`` / ``bioproject:PRJNA…``).
    """
    out: list[Link] = []
    for db in _ELINK_DBS:
        uids = await _eutils.elink(client, dbfrom="pubmed", db=db, ids=[pmid])
        if not uids:
            continue
        docs = await _eutils.esummary(client, db, uids)
        normalize = omics._NORMALIZERS[db]
        for doc in docs:
            out.append(Link(rel="has_data", target_id=normalize(doc).id))
    return out


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    """Resolve ``pubmed:<PMID>`` to a full record with elink-discovered data links
    and (when open access) an attached full-text file."""
    prefix, _, pmid = resource_id.partition(":")
    if prefix != "pubmed" or not pmid:
        raise NotFoundError(f"unroutable pubmed id {resource_id!r}")
    docs = await _eutils.esummary(client, "pubmed", [pmid])
    if not docs:
        raise NotFoundError(f"no pubmed record for {pmid!r}")
    resource = _normalize_pubmed(docs[0])
    links = await _links_via_elink(client, pmid)
    ft = await fulltext.find(client, pmcid=resource.identifiers.get("pmcid"), doi=resource.doi)
    abstract = await _abstract_for(client, pmid)
    update: dict = {}
    if links:
        update["links"] = links
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
