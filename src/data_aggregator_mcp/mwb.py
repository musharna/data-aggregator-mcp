"""Metabolomics Workbench — the NIH metabolomics data repository, searched natively.

The omicsdi source reaches the Workbench only through OmicsDI's copy, which stops at
ST002490 while the Workbench is at ST005288 (2,168 of 4,623 studies, probed
2026-10-11). In the round-3 benchmark two keyed coral heat-stress studies (ST004143,
ST004329) were past that copy, and dam never saw them; the Workbench's own title
search returns both for "coral".

The REST API (https://www.metabolomicsworkbench.org/tools/mw_rest.php) searches a study
by title, institute or surname only, as a case-insensitive substring of ONE string
("coral" also returns "Strongyloides stercoralis"). So each word of the query is sent
as its own title search, and a study is kept when its title, species, analysis type and
institute name every word as the server reads one (``_relevance``: whole words, a word
also naming its plural). A study whose title holds none of the words is out of reach;
OmicsDI's copy, which also indexes descriptions, still finds the older ones. The whole
list (``study_id/ST/summary``) would let every field be matched, but takes 91 s.

Discovery-only: a record names its study and landing page; Workbench file download is
not wired. Every study carries CC BY 4.0 (4,623 of 4,623).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx

from data_aggregator_mcp import _http
from data_aggregator_mcp._relevance import _names, holding_words, query_terms
from data_aggregator_mcp._relevance import _normalize as _normalize_words
from data_aggregator_mcp.errors import NotFoundError
from data_aggregator_mcp.license_compat import normalize_spdx
from data_aggregator_mcp.models import DataResource, Link, compact, local_id, year_from

API = "https://www.metabolomicsworkbench.org/rest/study"
_LANDING = "https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?Mode=Study&StudyID={id}"
_GET = "GET"
_ACCEPT_JSON = {"Accept": "application/json"}
PREFIXES = {"mwb"}
_ID_RE = re.compile(r"ST[0-9]{6}")
DEFAULT_SIZE = 10
MAX_SIZE = 50
# A study must name every word of the query, so the router names this source when a
# multi-word search empties it.
REQUIRES_EVERY_WORD = True
# Its studies naming only some of the words are searched too, below those naming every
# one (``router._source_streams``); matched here, so asked for with some_words.
ANY_WORD_FILL = True
FILLS_IN_CODE = True
# A substring of one or two letters is in most titles, and the answer's cost grows with
# its size ("metabolomics" 912 studies in 5.9 s, the whole list 91 s; 2026-10-11). Such a
# word is still required of every study kept; it is sent only when it is all there is.
_MIN_SENT = 3
# The search reads the summaries of every word's matches: the slowest of them decides.
DEFAULT_TIMEOUT = 60.0
# Several species come as one string: "Homo sapiens; Mus musculus", "A, B", "A；B".
_SPECIES_SEP = re.compile(r"\s*[,;；]\s*")
_TEXT_FIELDS = ("study_title", "species", "analysis_type", "institute")


def _is_study(s: object) -> bool:
    """A Workbench study summary, with every field ``_normalize`` reads at the type it
    reads it as (absent is fine for all but the id and title)."""
    return (
        isinstance(s, dict)
        and isinstance(s.get("study_id"), str)
        and _ID_RE.fullmatch(s["study_id"]) is not None
        and isinstance(s.get("study_title"), str)
        and all(
            isinstance(s.get(f), str | None)
            for f in ("species", "institute", "analysis_type", "release_date", "license")
        )
    )


def _studies(body: list | dict) -> list[dict]:
    """The studies of one answer, which comes in three shapes: ``[]`` for none, the
    study itself for one, and ``{"1": study, "2": study, ...}`` for several."""
    if isinstance(body, list):
        return list(body)
    if "study_id" in body:
        return [body]
    return list(body.values())


def _check(body: list | dict) -> None:
    if not all(_is_study(s) for s in _studies(body)):
        raise _http.UpstreamEnvelopeError(f"no Workbench study list in {body!r:.200}")


def _species(s: dict) -> list[str]:
    value = s.get("species") or ""
    return [p for p in _SPECIES_SEP.split(value.strip()) if p and p != "-"]


def _normalize(s: dict) -> DataResource:
    sid = s["study_id"]
    released = s.get("release_date") or None
    return DataResource(
        id=f"mwb:{sid}",
        source="mwb",
        kind="study",
        title=s["study_title"],
        year=year_from(released),
        accessions=[sid],
        organism=_species(s),
        subjects=[s["analysis_type"]] if s.get("analysis_type") else [],
        license=normalize_spdx(s["license"]) if s.get("license") else None,
        last_updated=released,
        # The record's ``study_url`` names the searched string, not the study
        # ("StudyID=CORAL"), so the landing page is built from the id.
        links=[Link(rel="landing_page", target_id=_LANDING.format(id=sid))],
    )


async def _get(client: httpx.AsyncClient, field: str, value: str, *, service: str):
    # A word holds letters, digits and spaces only (``query_terms``), and an id is checked
    # before it gets here, so no value has a "/" for `safe` to decide on; quoting every
    # character keeps it one path segment regardless.
    segment = quote(value, safe="")  # pragma: no mutate
    body = await _http.request_json(
        client,
        _GET,
        f"{API}/{field}/{segment}/summary",
        service=service,
        headers=_ACCEPT_JSON,
        expect=(list, dict),
        check=_check,
        timeout=DEFAULT_TIMEOUT,
    )
    return _studies(body)


def _text(s: dict) -> str:
    return _normalize_words(" ".join(v for f in _TEXT_FIELDS if isinstance(v := s.get(f), str)))


async def search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    offset: int = 0,
    some_words: bool = False,
) -> tuple[int, list[DataResource]]:
    """Workbench studies naming every word of ``query``, newest first; with
    ``some_words``, those naming only some of them, the ones naming more first: (total,
    compact resources)."""
    terms = query_terms(query)
    if not terms:
        raise ValueError(
            f"Metabolomics Workbench search needs a word to look for in titles; {query!r} has none"
        )
    sent = [t for t in terms if len(t) >= _MIN_SENT] or terms
    pages = await asyncio.gather(
        *(_get(client, "study_title", t, service="Metabolomics Workbench search") for t in sent)
    )
    studies = {s["study_id"]: s for page in pages for s in page}
    newest = sorted(studies.values(), key=lambda s: s["study_id"], reverse=True)
    matched = holding_words(newest, lambda s, t: _names(_text(s), t), terms, some_words=some_words)
    capped = min(size, MAX_SIZE)
    return len(matched), [compact(_normalize(s)) for s in matched[offset : offset + capped]]


async def resolve(client: httpx.AsyncClient, resource_id: str) -> DataResource:
    sid = local_id(resource_id, "mwb", strip=True, upper=True)
    if not _ID_RE.fullmatch(sid):
        raise NotFoundError(f"malformed Metabolomics Workbench study id {resource_id!r}")
    for s in await _get(client, "study_id", sid, service="Metabolomics Workbench resolve"):
        if s["study_id"] == sid:
            return _normalize(s)
    raise NotFoundError(f"Metabolomics Workbench has no study {sid}")
