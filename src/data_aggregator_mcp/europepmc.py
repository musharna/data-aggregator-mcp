"""Europe PMC text-mined accessions — the data deposits a paper names in its text.

NCBI's elink and OpenAIRE's Scholix link a paper to data only when someone recorded the
link; most deposits are named in the paper and nowhere else. Of the round-2 benchmark
studies no arm found, the BioProjects behind Boothby 2017 (PRJNA369152) and the snow
leopard virome paper (PRJNA626440) had no PubMed link either way, while Europe PMC's
annotations API, which mines accessions from open full text and author manuscripts,
named both (probed 2026-10-06).

One GET per paper (``annotationsByArticleIds``, ``type=Accession Numbers``). Each
annotated name is kept only when it is a whole data accession of an archive this server
can reach (``_ROUTES``), as the id the router resolves; a ProteomeXchange or MetaboLights
accession, which no single source here resolves, is kept as the bare accession. The rest
of what the miner tags (RRIDs, GO terms, UniProt and PDB entries, single GenBank
sequences) names reagents and reference records, not the paper's data. A paper names
others' data too (Boothby 2017 names two older SRA experiments), so the links say
``references``, never ``has_data``.

Enrichment: a failed or off-contract answer degrades to no links and names the reason,
as ``scholix.links_for`` does, so an outage never reads as "this paper names no data".
"""

from __future__ import annotations

import logging
import re

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp.errors import DataAggregatorError
from data_aggregator_mcp.models import Link

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
_GET = "GET"
_REL = "references"
# A paper can name thousands of accessions (every GSM of a series); the links are capped
# as elinked runs are (omics.MAX_LINKED_RUNS) and the cut is reported.
MAX_LINKS = 100

# Whole accession → the id a source here resolves ("{}" is the accession). Order
# matters only for readability: the patterns do not overlap.
_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"PRJ(?:NA|EB|DB)[0-9]+"), "bioproject:{}"),
    (re.compile(r"PRJCA[0-9]+"), "ngdc:{}"),
    (re.compile(r"G(?:SE|SM|PL|DS)[0-9]+"), "geo:{}"),
    # Study, experiment and run; ``sra:`` resolves an experiment and names the
    # experiments that carry a study or run.
    (re.compile(r"[SED]R[PXR][0-9]+"), "sra:{}"),
    (re.compile(r"E-[A-Z]{4}-[0-9]+"), "biostudies:{}"),
    (re.compile(r"GCST[0-9]+"), "gwas:{}"),
    (re.compile(r"PXD[0-9]+"), "{}"),
    (re.compile(r"MTBLS[0-9]+"), "{}"),
)


def _target(name: str) -> str | None:
    acc = name.strip().upper()
    for pattern, template in _ROUTES:
        if pattern.fullmatch(acc):
            return template.format(acc)
    return None


def merge(links: list[Link], mined: list[Link]) -> list[Link]:
    """``links`` then each mined link whose target none of them already names: a
    deposit elink or Scholix linked keeps its stronger rel."""
    seen = {link.target_id.lower() for link in links}
    return links + [m for m in mined if m.target_id.lower() not in seen]


def _is_annotation(a: object) -> bool:
    """An annotation with an ``exact`` string and, when present, a list of tag objects
    each naming a string."""
    if not (isinstance(a, dict) and isinstance(a.get("exact"), str)):
        return False
    tags = a.get("tags")
    return tags is None or (
        isinstance(tags, list)
        and all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tags)
    )


def _check_answer(body: list) -> None:
    """A list of articles, each with a list of annotations (``_is_annotation``). An
    article Europe PMC does not know answers ``[]``."""
    if not all(
        isinstance(art, dict)
        and isinstance(art.get("annotations"), list)
        and all(_is_annotation(a) for a in art["annotations"])
        for art in body
    ):
        raise _http.UpstreamEnvelopeError(f"no list of annotated articles in {body!r:.200}")


async def mined_links(
    client: httpx.AsyncClient, *, pmid: str | None = None, pmcid: str | None = None
) -> tuple[list[Link], str | None, str | None]:
    """The data accessions the paper names, as ``references`` links (first-named first,
    each once); a truncation note when capped; and why they are unknown (None when
    the lookup answered). No PMID or PMCID → nothing to ask."""
    if pmid:
        article = f"MED:{pmid}"
    elif pmcid:
        article = f"PMC:{pmcid.upper()}"
    else:
        return [], None, None
    try:
        body = await _http.request_json(
            client,
            _GET,
            BASE_URL,
            service="Europe PMC annotations",
            params={"articleIds": article, "type": "Accession Numbers", "format": "JSON"},
            expect=list,
            check=_check_answer,
        )
    except DataAggregatorError as exc:
        logger.warning("Europe PMC annotations lookup failed for %s: %s", article, exc)
        return (
            [],
            None,
            f"Europe PMC annotations lookup failed ({type(exc).__name__}: {exc}); "
            "data accessions named in the text unknown",
        )
    targets: list[str] = []
    for art in body:
        for a in art["annotations"]:
            names = [t["name"] for t in a.get("tags") or []] or [a["exact"]]
            targets.extend(t for t in map(_target, names) if t)
    unique = list(dict.fromkeys(targets))
    cut = (
        f"first {MAX_LINKS} of {len(unique)} data accessions named in the text"
        if len(unique) > MAX_LINKS
        else None
    )
    return [Link(rel=_REL, target_id=t) for t in unique[:MAX_LINKS]], cut, None
