"""NCBI Taxonomy lookups: name -> canonical taxon + synonyms + plant flag.

Internal helper (NOT a router adapter). Backs Phase 5 synonym expansion (search
input) and organism normalization (result enrichment). One source serves both:
esearch db=taxonomy resolves a name to a taxid; efetch (XML) yields the canonical
ScientificName, OtherNames/Synonym list, and the Lineage (for a Viridiplantae
plant flag). Results are cached in-process keyed by lowercased name.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from data_aggregator_mcp import _eutils


@dataclass(frozen=True)
class TaxonInfo:
    taxid: int
    canonical_name: str
    synonyms: tuple[str, ...]
    is_plant: bool


def _parse_taxon(xml_text: str) -> TaxonInfo | None:
    """Parse a taxonomy efetch ``<TaxaSet>`` body; first ``<Taxon>`` or None."""
    root = ET.fromstring(xml_text)
    taxon = root.find("Taxon")
    if taxon is None:
        return None
    taxid_text = taxon.findtext("TaxId")
    canonical = taxon.findtext("ScientificName")
    if not taxid_text or not canonical:
        return None
    synonyms = tuple(s.text for s in taxon.findall("OtherNames/Synonym") if s.text)
    lineage = taxon.findtext("Lineage") or ""
    is_plant = "Viridiplantae" in {part.strip() for part in lineage.split(";")}
    return TaxonInfo(
        taxid=int(taxid_text),
        canonical_name=canonical,
        synonyms=synonyms,
        is_plant=is_plant,
    )


_CACHE: dict[str, TaxonInfo | None] = {}


async def resolve_taxon(client: httpx.AsyncClient, name: str) -> TaxonInfo | None:
    """Resolve an organism ``name`` to a ``TaxonInfo`` (or None if no match).

    Cached in-process by lowercased name (negative results cached too) so
    repeated organisms in one request cost a single NCBI round-trip. HTTP
    failures propagate (the caller surfaces them); they are NOT cached.
    """
    key = name.strip().lower()
    if not key:
        return None
    if key in _CACHE:
        return _CACHE[key]
    _count, ids = await _eutils.esearch(client, "taxonomy", name, retmax=1)
    if not ids:
        _CACHE[key] = None
        return None
    xml_text = await _eutils.efetch(client, "taxonomy", [ids[0]], retmode="xml")
    info = _parse_taxon(xml_text)
    _CACHE[key] = info
    return info
