"""NCBI Taxonomy lookups: name -> canonical taxon + synonyms + plant flag.

Internal helper (NOT a router adapter). Backs Phase 5 synonym expansion (search
input) and organism normalization (result enrichment). One source serves both:
esearch db=taxonomy resolves a name to a taxid; efetch (XML) yields the canonical
ScientificName, OtherNames/Synonym list, and the Lineage (for a Viridiplantae
plant flag). Results are cached in-process keyed by lowercased name.

A name can match several taxa ("Drosophila" is a fly genus, a fly subgenus and a fungus
genus; "fruit fly" is the common name of five), and esearch lists them by descending
taxid, so its first hit was arbitrary: "Drosophila" resolved to the fungus. The taxon
chosen is the one whose scientific name the name is, if exactly one is; otherwise the
best-known, by the nucleotide records NCBI annotates to it. The others are kept on
``TaxonInfo.alternatives`` so the choice is visible (user decision 2026-10-04).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import httpx
from defusedxml import ElementTree as ET  # remote XML: entity-expansion safe

from data_aggregator_mcp import _eutils
from data_aggregator_mcp._cache import MISS, TTLCache
from data_aggregator_mcp._relevance import _pluralisable
from data_aggregator_mcp.errors import UpstreamUnavailableError


@dataclass(frozen=True)
class TaxonCandidate:
    """Another taxon the name matched, not chosen."""

    taxid: int
    name: str


@dataclass(frozen=True)
class TaxonInfo:
    taxid: int
    canonical_name: str
    synonyms: tuple[str, ...]
    is_plant: bool
    alternatives: tuple[TaxonCandidate, ...] = ()


def _parse_taxon(xml_text: str) -> TaxonInfo | None:
    """The first ``<Taxon>`` of a taxonomy efetch body, or None when the set is empty
    (``_parse_taxa``)."""
    taxa = _parse_taxa(xml_text)
    return taxa[0] if taxa else None


def _parse_taxa(xml_text: str) -> list[TaxonInfo]:
    """Parse a taxonomy efetch ``<TaxaSet>`` body: every ``<Taxon>``, in order; empty when
    the set is (NCBI's answer for an id with no record; live 2026-10-02, id 999999999).

    Anything else is an off-contract answer, raised as an upstream failure (like the
    HTTP failures resolve_taxon already propagates, and uncached), never read as "no
    such taxon" and cached for an hour: efetch's error envelope ``<eFetchResult><ERROR>``
    (live with HTTP 400 for id 0) inside a 200, or a ``<Taxon>`` without its TaxId or
    ScientificName (every one of 400 live taxa has both).
    """
    root = ET.fromstring(xml_text)
    if root.tag != "TaxaSet":
        error = (root.findtext("ERROR") or "").strip()
        raise UpstreamUnavailableError(
            f"NCBI taxonomy efetch answered <{root.tag}>, not <TaxaSet>: {error!r}"
        )
    return [_taxon(taxon) for taxon in root.findall("Taxon")]


def _taxon(taxon: ET.Element) -> TaxonInfo:
    taxid_text = taxon.findtext("TaxId")
    canonical = taxon.findtext("ScientificName")
    if not taxid_text or not canonical:
        raise UpstreamUnavailableError(
            "NCBI taxonomy answered a Taxon without a TaxId or ScientificName "
            f"(TaxId={taxid_text!r}, ScientificName={canonical!r})"
        )
    synonyms = tuple(s.text for s in taxon.findall("OtherNames/Synonym") if s.text)
    try:
        taxid = int(taxid_text)
    except ValueError:
        # Off-contract NCBI answer. Raised as an upstream failure (like the HTTP failures
        # resolve_taxon already propagates, and uncached), not int()'s bare ValueError.
        # (Not str.isdigit(): it accepts "²", which int() rejects.)
        raise UpstreamUnavailableError(
            f"NCBI taxonomy answered a non-numeric TaxId {taxid_text!r} for {canonical!r}"
        ) from None
    lineage = taxon.findtext("Lineage")
    is_plant = lineage is not None and "Viridiplantae" in {p.strip() for p in lineage.split(";")}
    return TaxonInfo(
        taxid=taxid,
        canonical_name=canonical,
        synonyms=synonyms,
        is_plant=is_plant,
    )


# Candidates read per name. NCBI's own lists for common names hold 1 to 4 (fruit fly 4,
# Drosophila 3, mouse/rat/maize/bacteria 2; live 2026-10-04); a name matching more is
# judged among the first 10.
_MAX_CANDIDATES = 10


async def _records(client: httpx.AsyncClient, taxid: int, scope: str) -> int:
    """Nucleotide records NCBI annotates to ``taxid`` itself (``noexp``) or to it and its
    descendants (``exp``)."""
    count, _ids = await _eutils.esearch(
        client, "nuccore", f"txid{taxid}[Organism:{scope}]", retmax=0
    )
    return count


async def _best_known(client: httpx.AsyncClient, key: str, taxa: list[TaxonInfo]) -> TaxonInfo:
    """The taxon ``key`` names: the one whose scientific name it is, if exactly one is;
    else, among those (or all, if none is), the one with the most nucleotide records
    annotated to the taxon itself. Counting descendants too would pick the genus Mus over
    the house mouse for "mouse" (11.35 M vs 11.21 M); counted on the taxon itself it is
    10.6 M vs 2,375. A tie at the top (homonyms with no records of their own) goes to the
    count with descendants, then to the smaller taxid, so the choice is reproducible."""
    exact = [t for t in taxa if t.canonical_name.lower() == key]
    if len(exact) == 1:
        return exact[0]
    pool = exact or taxa
    own = {t.taxid: await _records(client, t.taxid, "noexp") for t in pool}
    top = [t for t in pool if own[t.taxid] == max(own.values())]
    if len(top) == 1:
        return top[0]
    wide = {t.taxid: await _records(client, t.taxid, "exp") for t in top}
    return min(top, key=lambda t: (-wide[t.taxid], t.taxid))


def _name_term(words: list[str]) -> str:
    """The esearch term for a name of ``words``, asked for as a whole name and with its
    last word plural: ``"water bear"[All Names] OR "water bears"[All Names]``. Sent as
    written, esearch dropped a phrase it could not find and searched what was left:
    "water bear" became ``bear[All Names]`` and resolved to Ursus sp. And NCBI names
    Tardigrada only in the plural ("tardigrades", "water bears"), so "tardigrade" matched
    nothing (2026-10-07)."""
    forms = [" ".join(words)]
    if _pluralisable(words[-1]):
        forms.append(" ".join([*words[:-1], words[-1] + "s"]))
    return " OR ".join(f'"{form}"[All Names]' for form in forms)


_NEG = object()  # cached "no match" (distinct from a missing key)
_CACHE = TTLCache(maxsize=4096, ttl=3600.0)


async def resolve_taxon(client: httpx.AsyncClient, name: str) -> TaxonInfo | None:
    """Resolve an organism ``name`` to a ``TaxonInfo`` (or None if no match).

    Cached in-process by lowercased name (negative results cached too) so
    repeated organisms in one request cost a single NCBI round-trip. HTTP
    failures propagate (the caller surfaces them); they are NOT cached.
    """
    key = name.strip().lower()
    words = name.replace('"', " ").split()
    if not words:
        return None
    cached = _CACHE.get(key)
    if cached is not MISS:
        return None if cached is _NEG else cached
    term = _name_term(words)
    _count, ids = await _eutils.esearch(client, "taxonomy", term, retmax=_MAX_CANDIDATES)
    if not ids:
        _CACHE.set(key, _NEG)
        return None
    taxa = _parse_taxa(await _eutils.efetch(client, "taxonomy", ids))  # XML, efetch's default
    if not taxa:
        _CACHE.set(key, _NEG)
        return None
    chosen = taxa[0] if len(taxa) == 1 else await _best_known(client, key, taxa)
    others = tuple(TaxonCandidate(t.taxid, t.canonical_name) for t in taxa if t is not chosen)
    info = replace(chosen, alternatives=others)
    _CACHE.set(key, info)
    return info
